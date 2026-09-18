"""
watcher.py — Filesystem event handler for the SD card / folder-drop mode.

Uses the watchdog library which wraps OS-native file-system notification APIs:
  Linux  → inotify
  macOS  → FSEvents
  Windows → ReadDirectoryChangesW

No polling. The OS wakes our process the instant a new file appears in the
inbox directory. A short debounce (DEBOUNCE_SECONDS) ensures we do not read
the file before the OS has finished writing it — important for large RAW files
copied from an SD card reader.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileCreatedEvent
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# Seconds to wait after a file-creation event before treating the file as
# complete. This prevents reading a partial file while it is still being
# written (e.g. during SD card transfer of a 25 MB RAW file).
DEBOUNCE_SECONDS = 1.0


class ImageEventHandler(FileSystemEventHandler):
    """
    Handles 'file created' events from watchdog.

    When a new file appears in the watched directory:
      1. Waits for the debounce period
      2. Confirms the file still exists (not a transient temp file)
      3. Calls the provided on_new_file callback
    """

    def __init__(self, on_new_file: Callable[[Path], None]) -> None:
        super().__init__()
        self._on_new_file = on_new_file
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return

        src = event.src_path

        # Ignore hidden/temp files (e.g. .DS_Store, .Trashes, partial writes)
        if Path(src).name.startswith("."):
            return

        with self._lock:
            if src in self._pending:
                return
            self._pending.add(src)

        # Schedule the actual callback after the debounce delay
        t = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=[src])
        t.daemon = True
        t.start()

    def _fire(self, src: str) -> None:
        with self._lock:
            self._pending.discard(src)

        path = Path(src)
        if path.exists():
            logger.info("Watcher: new file detected → %s", path.name)
            self._on_new_file(path)
        else:
            logger.debug("Watcher: file disappeared before processing — ignored: %s", src)


def start_watcher(inbox: Path, on_new_file: Callable[[Path], None]) -> Observer:
    """
    Start a watchdog Observer on the inbox directory.

    Returns the Observer so the caller can stop it on shutdown.
    The observer runs in a daemon thread and does not block.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    handler = ImageEventHandler(on_new_file)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()
    logger.info("Watcher started — monitoring: %s", inbox.resolve())
    return observer
