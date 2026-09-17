"""Watch folders for new images.

This is the universal fallback, and in practice the most compatible path of all:
every vendor's tether utility (Canon EOS Utility, Nikon NX Tether, Sony Imaging
Edge) can be pointed at a folder, and so can a camera's own Wi-Fi/FTP transfer.
When a body is too new or too old for the MTP path, this still works.

Files already present when the bridge starts are deliberately ignored - opening
the app should not re-chart yesterday's shoot. Only images that arrive while the
bridge is running are captured.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..config import config
from ..models import CapturedImage
from .base import CaptureSource, Emit
from .fileutil import file_mtime_iso, read_when_stable


class _NewImageHandler(FileSystemEventHandler):
    """Feeds candidate paths to the source thread; does no I/O of its own.

    watchdog dispatches on its own thread, and reading a multi-megabyte RAW
    there would stall further events, so the handler only enqueues.
    """

    def __init__(self, pending: queue.Queue[Path]) -> None:
        self.pending = pending

    def _offer(self, path_str: str) -> None:
        path = Path(path_str)
        if config.is_image(path.name):
            self.pending.put(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Vendor software often writes to a .tmp name and renames on completion.
        if not event.is_directory:
            self._offer(str(event.dest_path))


class FolderWatchSource(CaptureSource):
    name = "folder"
    description = "Watches tether / Wi-Fi import folders for new images"

    def __init__(self, folders: list[Path] | None = None) -> None:
        super().__init__()
        self.folders = folders if folders is not None else config.watch_folders

    def probe(self) -> tuple[bool, str]:
        if not self.folders:
            return False, "no WATCH_FOLDERS configured"
        for folder in self.folders:
            folder.mkdir(parents=True, exist_ok=True)
        names = ", ".join(str(f) for f in self.folders)
        return True, f"watching {names}"

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        pending: queue.Queue[Path] = queue.Queue()
        handler = _NewImageHandler(pending)
        observer = Observer()

        baseline = 0
        for folder in self.folders:
            folder.mkdir(parents=True, exist_ok=True)
            baseline += sum(1 for p in folder.rglob("*") if p.is_file() and config.is_image(p.name))
            observer.schedule(handler, str(folder), recursive=True)

        observer.start()
        self.note(f"watching {len(self.folders)} folder(s); "
                  f"{baseline} pre-existing image(s) ignored", available=True)
        self.log.info("watching %s", ", ".join(str(f) for f in self.folders))

        try:
            while not stop_event.is_set():
                try:
                    path = pending.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._ingest(path, emit)
        finally:
            observer.stop()
            observer.join(timeout=5)
            self.note("stopped", available=False)

    def _ingest(self, path: Path, emit: Emit) -> None:
        data = read_when_stable(path)
        if data is None:
            self.log.warning("gave up reading %s", path)
            return
        emit(
            CapturedImage(
                filename=path.name,
                data=data,
                source=self.name,
                captured_at=file_mtime_iso(path),
                origin=str(path),
            )
        )
        self.count_capture()
        self.note(f"last capture: {path.name}", available=True)
