"""Auto-import from an SD card or a camera in USB mass-storage mode.

Covers the two cases MTP does not: a clinician who pops the card into a reader,
and older bodies that present themselves as a plain removable drive. A card
holding a DCIM folder is watched exactly like a tethered camera, and the same
rule decides what comes off it: the photographs taken during the capture
session, and nothing else. The card never has to be browsed by hand, and the
months of shots already on it are left where they are.
"""

from __future__ import annotations

import ctypes
import string
import threading
import time
from pathlib import Path

from ..config import config
from ..models import CapturedImage
from ..window import SKIP, TAKE
from .base import CaptureSource, Emit
from .fileutil import file_mtime_iso, read_when_stable

DRIVE_REMOVABLE = 2

CAMERA_DIRS = ("DCIM", "PRIVATE")


def removable_drives() -> list[Path]:
    """Drive letters Windows reports as removable volumes."""
    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    drives: list[Path] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_REMOVABLE:
            drives.append(Path(root))
    return drives


def camera_folders(drive: Path) -> list[Path]:
    return [drive / name for name in CAMERA_DIRS if (drive / name).is_dir()]


class RemovableMediaSource(CaptureSource):
    name = "removable"
    description = "Imports DCIM from SD cards and mass-storage cameras"

    def __init__(self) -> None:
        super().__init__()
        # Files settled for good, per volume. A card left in the reader is
        # rescanned every poll - it has to be, because the shot that matters is
        # the one taken a moment ago - but a file judged once is never read
        # again, so the rescan only ever looks at what is new.
        self._known: dict[str, set[str]] = {}

    def probe(self) -> tuple[bool, str]:
        if not hasattr(ctypes, "windll"):
            return False, "removable-media import is Windows-only"
        return True, "waiting for a card or camera volume"

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        self.note("waiting for a card or camera volume", available=True)
        while not stop_event.is_set():
            try:
                self._scan(emit, stop_event)
            except Exception as exc:  # a yanked card mid-scan must not kill the source
                self.log.warning("scan failed: %s", exc)
            stop_event.wait(config.poll_interval)
        self.note("stopped", available=False)

    def _scan(self, emit: Emit, stop_event: threading.Event) -> None:
        volumes = [d for d in removable_drives() if camera_folders(d)]
        if not volumes:
            self.note("waiting for a card or camera volume", available=True, device=None)
            self._known.clear()
            return

        present = set()
        for drive in volumes:
            key = str(drive)
            present.add(key)
            known = self._known.setdefault(key, set())
            pulled = self._import(camera_folders(drive), known, emit, stop_event)
            self.note(f"{key} - {self.window.describe()}"
                      + (f", {pulled} taken this sweep" if pulled else ""),
                      available=True, device=key)

        # Forget removed volumes, so re-inserting a card starts clean.
        for key in set(self._known) - present:
            del self._known[key]

    def _import(self, folders: list[Path], known: set[str], emit: Emit,
                stop_event: threading.Event) -> int:
        pulled = 0
        for folder in folders:
            for path in sorted(folder.rglob("*")):
                if stop_event.is_set():
                    return pulled
                key = str(path)
                if key in known:
                    continue
                if not path.is_file() or not config.is_image(path.name):
                    known.add(key)
                    continue

                captured_at = file_mtime_iso(path)
                verdict = self.window.verdict(captured_at)
                if verdict != TAKE:
                    if verdict == SKIP:
                        known.add(key)
                    continue

                data = read_when_stable(path, timeout=10)
                if data is None:
                    continue
                known.add(key)
                emit(
                    CapturedImage(
                        filename=path.name,
                        data=data,
                        source=self.name,
                        captured_at=captured_at,
                        origin=key,
                    )
                )
                self.count_capture()
                pulled += 1
                # Cards are fast but the backend is not; a small pause keeps the
                # upload queue moving instead of spiking to hundreds of items.
                time.sleep(0.05)
        if pulled:
            self.log.info("imported %d image(s) taken during this session", pulled)
        return pulled
