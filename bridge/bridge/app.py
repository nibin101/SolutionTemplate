"""The background service itself: supervise capture sources, drain the spool.

Threads, and why each one exists:

* one per capture source - each transport blocks in its own way (watchdog,
  COM polling, an SDK event pump), so they cannot share a loop;
* one uploader - the only thread that touches the network, so retry and
  back-off logic lives in exactly one place;
* one heartbeat - tells the chair-side UI the camera link is alive, and brings
  back the capture window every source filters by.

The supervisor restarts a source that dies. A crashed USB stack or a camera
yanked mid-transfer therefore costs one retry interval, not the whole service.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from . import __version__
from .config import config
from .logging_setup import setup_logging
from .models import CapturedImage
from .sources import CaptureSource, build_sources
from .spool import Spool
from .uploader import Uploader
from .window import CaptureWindow

log = setup_logging().getChild("app")

RESTART_BACKOFF_START = 5.0
RESTART_BACKOFF_MAX = 60.0


class BridgeApp:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        # Pause is a clinical control, not a technical one: it lets a clinician
        # take non-patient photos (equipment, a product shot) without them
        # landing in somebody's chart.
        self.paused = threading.Event()

        # One window, shared with every source. The heartbeat thread writes it;
        # the sources read it to decide what on the camera is clinical.
        self.window = CaptureWindow()

        self.spool = Spool()
        self.sources: list[CaptureSource] = build_sources(config.enabled_sources)
        for source in self.sources:
            source.window = self.window
        self.uploader = Uploader(self.spool, self.stop_event)
        self.threads: list[threading.Thread] = []
        self.started_at = time.time()
        self.dropped_while_paused = 0

    # -- capture callback --------------------------------------------------

    def emit(self, captured: CapturedImage) -> None:
        if self.paused.is_set():
            self.dropped_while_paused += 1
            log.info("paused - ignoring %s from %s", captured.filename, captured.source)
            return
        self.spool.enqueue(captured)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        log.info("SnapChart bridge %s starting (operatory=%s, backend=%s)",
                 __version__, config.operatory, config.backend_url)

        if not self.sources:
            log.error("no capture sources enabled - check ENABLED_SOURCES in bridge/.env")

        for source in self.sources:
            thread = threading.Thread(
                target=self._supervise, args=(source,), name=f"source-{source.name}", daemon=True
            )
            thread.start()
            self.threads.append(thread)

        self.uploader.start()
        self.threads.append(self.uploader)

        heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        heartbeat.start()
        self.threads.append(heartbeat)

    def stop(self) -> None:
        log.info("bridge stopping")
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=5)
        log.info("bridge stopped")

    def wait(self) -> None:
        """Block the main thread until stopped (headless mode)."""
        try:
            while not self.stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    # -- supervision -------------------------------------------------------

    def _supervise(self, source: CaptureSource) -> None:
        backoff = RESTART_BACKOFF_START
        while not self.stop_event.is_set():
            usable, detail = self._probe(source)
            source.note(detail, available=usable)
            if not usable:
                log.warning("source %s unavailable: %s", source.name, detail)
                return

            log.info("source %s: %s", source.name, detail)
            try:
                source.run(self.stop_event, self.emit)
                if self.stop_event.is_set():
                    return
                # A clean return without a stop request means the transport went
                # away (camera unplugged); treat it like a crash and retry.
                log.info("source %s ended; restarting in %.0fs", source.name, backoff)
            except NotImplementedError as exc:
                log.warning("source %s is not implemented: %s", source.name, exc)
                return
            except Exception as exc:
                log.exception("source %s crashed: %s", source.name, exc)
                source.note(f"restarting after error: {exc}", available=False)

            if self.stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX)

    @staticmethod
    def _probe(source: CaptureSource) -> tuple[bool, str]:
        try:
            return source.probe()
        except Exception as exc:
            return False, f"probe failed: {exc}"

    # -- heartbeat ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        stats = self.spool.stats()
        return {
            "version": __version__,
            "operatory": config.operatory,
            "paused": self.paused.is_set(),
            "uptime_seconds": int(time.time() - self.started_at),
            "queue": stats,
            "last_error": self.uploader.last_error,
            "dropped_while_paused": self.dropped_while_paused,
            "sources": [source.status().as_dict() for source in self.sources],
        }

    def _heartbeat_loop(self) -> None:
        """Say we are alive; learn who, if anyone, is in the chair.

        The reply carries the capture window - the start and end of the session
        currently running in this operatory. That is the only instruction the
        bridge ever receives, and it names no patient.
        """
        session = requests.Session()
        while not self.stop_event.is_set():
            try:
                response = session.post(
                    config.heartbeat_url,
                    headers={"X-Bridge-Token": config.bridge_token},
                    json=self.status(),
                    timeout=10,
                )
                if response.status_code == 200:
                    self._apply_window(response.json().get("window"))
            except requests.RequestException as exc:
                # Expected whenever the backend is down. The last known window
                # stands: capture keeps running into the spool, and the server
                # re-checks every timestamp at ingest anyway.
                log.debug("heartbeat failed: %s", exc)
            except ValueError:
                log.debug("heartbeat reply was not JSON")
            self.stop_event.wait(config.heartbeat_seconds)

    def _apply_window(self, payload: dict | None) -> None:
        if self.window.update(payload):
            log.info("capture window: %s", self.window.describe())
