"""USB MTP capture through the Windows Shell - the fallback for `wpd`.

Same outcome as `wpd_ptp`, reached a different way: the Shell already knows how
to talk to every portable device Explorer can show, so we ask it to copy new
camera files into a staging folder instead of driving the COM streams ourselves.
It is slower and less precise than WPD, but it tends to cope with bodies whose
MTP implementation is unusual. Enable it with ENABLED_SOURCES=shell when a
particular camera will not transfer over `wpd`.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import config
from ..models import CapturedImage
from ..window import SKIP, TAKE
from .base import CaptureSource, Emit
from .fileutil import file_mtime_iso, read_when_stable

SSF_DRIVES = 17

# Silent copy: no progress dialog, no confirmations, no undo record.
COPY_FLAGS = 4 | 16 | 512 | 1024

MIN_POLL_SECONDS = 4.0

# Shell folders for real disks have a filesystem path; MTP devices do not, which
# is exactly how we tell a camera apart from a hard drive.
def _is_portable_device(item) -> bool:
    try:
        return bool(item.IsFolder) and not Path(str(item.Path)).drive
    except Exception:
        return False


def _item_date(item) -> str | None:
    """A Shell item's modified date as ISO-8601 UTC, or None if it has none.

    The Shell reports it in the machine's local time with no zone attached,
    which is also how a camera writes it, so it is read as local and converted.
    """
    try:
        modified = item.ModifyDate
        naive = datetime(modified.year, modified.month, modified.day,
                         modified.hour, modified.minute, modified.second)
    except Exception:
        return None
    return naive.astimezone().astimezone(timezone.utc).replace(microsecond=0).isoformat()


class ShellMtpSource(CaptureSource):
    name = "shell"
    description = "USB MTP fallback that copies via the Windows Shell"

    def __init__(self) -> None:
        super().__init__()
        self._known: dict[str, set[str]] = {}

    def probe(self) -> tuple[bool, str]:
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return False, "pywin32 is not installed"
        return True, "ready - will pick up a camera when plugged in"

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        staging = Path(tempfile.mkdtemp(prefix="snapchart-mtp-"))
        interval = max(config.poll_interval, MIN_POLL_SECONDS)
        self.note("scanning for cameras", available=True)
        try:
            while not stop_event.is_set():
                try:
                    shell = win32com.client.Dispatch("Shell.Application")
                    self._sweep(shell, staging, emit, stop_event)
                except Exception as exc:
                    self.log.warning("sweep failed: %s", exc)
                    self.note(f"recovering: {exc}", available=True)
                stop_event.wait(interval)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            pythoncom.CoUninitialize()
            self.note("stopped", available=False)

    def _sweep(self, shell, staging: Path, emit: Emit,
               stop_event: threading.Event) -> None:
        computer = shell.NameSpace(SSF_DRIVES)
        if computer is None:
            return

        devices = [item for item in computer.Items() if _is_portable_device(item)]
        if not devices:
            self.note("no camera connected", available=True, device=None)
            return

        for device in devices:
            if stop_event.is_set():
                return
            name = str(device.Name)
            known = self._known.setdefault(name, set())
            pulled = self._walk(device.GetFolder, staging, known, name, emit,
                                stop_event)
            self.note(f"{name} - {self.window.describe()}"
                      + (f", {pulled} taken this sweep" if pulled else ""),
                      available=True, device=name)

    def _walk(self, folder, staging: Path, known: set[str], device_name: str,
              emit: Emit, stop_event: threading.Event) -> int:
        pulled = 0
        try:
            items = list(folder.Items())
        except Exception:
            return 0

        for item in items:
            if stop_event.is_set():
                return pulled
            try:
                item_name = str(item.Name)
                is_folder = bool(item.IsFolder)
            except Exception:
                continue

            if is_folder:
                pulled += self._walk(item.GetFolder, staging, known,
                                     device_name, emit, stop_event)
                continue

            key = f"{folder.Title}/{item_name}"
            if key in known or not config.is_image(item_name):
                known.add(key)
                continue

            # Same rule as every other source: was it taken while this patient
            # was in the chair? The Shell gives us a modified date without
            # copying anything, so this costs nothing to ask.
            verdict = self.window.verdict(_item_date(item))
            if verdict != TAKE:
                if verdict == SKIP:
                    known.add(key)
                continue

            known.add(key)
            if self._copy_and_emit(item, item_name, staging, device_name, emit):
                pulled += 1
        return pulled

    def _copy_and_emit(self, item, item_name: str, staging: Path,
                       device_name: str, emit: Emit) -> bool:
        import win32com.client

        destination = staging / item_name
        destination.unlink(missing_ok=True)
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            shell.NameSpace(str(staging)).CopyHere(item, COPY_FLAGS)
        except Exception as exc:
            self.log.warning("copy of %s failed: %s", item_name, exc)
            return False

        # CopyHere is asynchronous: the call returns before the bytes land.
        data = read_when_stable(destination, timeout=60)
        if data is None:
            self.log.warning("timed out waiting for %s", item_name)
            return False

        captured_at = file_mtime_iso(destination)
        destination.unlink(missing_ok=True)

        emit(
            CapturedImage(
                filename=item_name,
                data=data,
                source=self.name,
                captured_at=captured_at,
                camera_model=device_name,
                origin=f"shell://{device_name}/{item_name}",
            )
        )
        self.count_capture()
        return True

