"""Shared file helpers for the disk-based capture sources."""

from __future__ import annotations

import time
from pathlib import Path

# A 24 MP JPEG written over USB 2 can take a second or two to land, and a RAW
# file longer. Reading it too early yields a truncated image, so we wait for the
# size to stop changing before treating the file as a finished capture.
STABLE_CHECKS = 2
STABLE_INTERVAL = 0.4
STABLE_TIMEOUT = 30.0


def read_when_stable(path: Path, timeout: float = STABLE_TIMEOUT) -> bytes | None:
    """Return the file's bytes once it has stopped growing, or None on failure."""
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_for = 0

    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return None

        if size > 0 and size == last_size:
            stable_for += 1
            if stable_for >= STABLE_CHECKS:
                try:
                    return path.read_bytes()
                except PermissionError:
                    # Still held open by the transfer software - keep waiting.
                    stable_for = 0
                except OSError:
                    return None
        else:
            stable_for = 0

        last_size = size
        time.sleep(STABLE_INTERVAL)

    return None


def file_mtime_iso(path: Path) -> str | None:
    from datetime import datetime, timezone

    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).replace(microsecond=0).isoformat()
