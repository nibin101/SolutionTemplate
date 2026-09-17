"""Drains the spool into the backend.

One worker thread, oldest capture first. The uploader is the only component
that talks to the network, so every network failure mode is handled in one
readable place.
"""

from __future__ import annotations

import threading

import requests

from .config import config
from .logging_setup import setup_logging
from .spool import Spool, SpoolItem

log = setup_logging().getChild("uploader")

IDLE_SLEEP = 1.0
UPLOAD_TIMEOUT = (10, 120)  # (connect, read) seconds - RAW files are large

# Status codes that mean "this request will never succeed, stop burning retries".
PERMANENT_STATUSES = {400, 403, 404, 413, 415, 422}


class Uploader(threading.Thread):
    def __init__(self, spool: Spool, stop_event: threading.Event) -> None:
        super().__init__(name="uploader", daemon=True)
        self.spool = spool
        self.stop_event = stop_event
        self.session = requests.Session()
        self.last_error: str | None = None
        self.online = False

    def run(self) -> None:
        log.info("uploader started -> %s", config.ingest_url)
        while not self.stop_event.is_set():
            items = self.spool.due_items()
            if not items:
                self.stop_event.wait(IDLE_SLEEP)
                continue
            for item in items:
                if self.stop_event.is_set():
                    break
                self._deliver(item)

    def _deliver(self, item: SpoolItem) -> None:
        try:
            payload = item.read_bytes()
        except OSError as exc:
            # The blob vanished (antivirus, manual cleanup). Retrying cannot fix
            # it, so park the item instead of looping forever.
            self.spool.mark_failed(item, f"spooled file unreadable: {exc}")
            return

        meta = item.meta
        form = {
            "source": meta.get("source", "unknown"),
            "operatory": meta.get("operatory", config.operatory),
            "content_hash": item.content_hash,
        }
        # captured_at is the field the backend routes on - it is how a
        # photograph finds the patient who was in the chair when it was taken.
        for key in ("captured_at", "camera_make", "camera_model"):
            if meta.get(key):
                form[key] = meta[key]

        try:
            response = self.session.post(
                config.ingest_url,
                headers={"X-Bridge-Token": config.bridge_token},
                files={"file": (item.filename, payload, _mime_for(item.filename))},
                data=form,
                timeout=UPLOAD_TIMEOUT,
            )
        except requests.RequestException as exc:
            self.online = False
            self.last_error = str(exc)
            self.spool.mark_retry(item, f"network error: {exc}")
            return

        self.online = True

        if response.status_code == 401:
            # Misconfigured token: retrying is correct (the operator can fix the
            # .env and the queue drains), but it must be loud in the log.
            self.last_error = "backend rejected the bridge token (check BRIDGE_TOKEN)"
            self.spool.mark_retry(item, self.last_error)
            return

        if response.status_code in PERMANENT_STATUSES:
            self.spool.mark_failed(item, f"backend refused ({response.status_code}): "
                                         f"{response.text[:200]}")
            return

        if response.status_code >= 400:
            self.last_error = f"backend error {response.status_code}"
            self.spool.mark_retry(item, f"{self.last_error}: {response.text[:200]}")
            return

        body = _safe_json(response)
        status = body.get("status", "stored")
        if status == "rejected":
            self.spool.mark_failed(item, f"rejected: {body.get('reason', 'unknown')}")
            return

        # "duplicate" means the backend already has these bytes - that is a
        # success from the bridge's point of view, and exactly what a retry of a
        # request that actually landed looks like.
        self.last_error = None
        self.spool.mark_sent(item, disposition=status)


def _safe_json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _mime_for(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".tif", ".tiff")):
        return "image/tiff"
    if lowered.endswith(".heic"):
        return "image/heic"
    if lowered.endswith((".cr2", ".cr3", ".nef", ".arw")):
        # RAW files keep their bytes; the backend stores them and simply has no
        # preview for them, which is better than refusing a clinical capture.
        return "application/octet-stream"
    return "image/jpeg"
