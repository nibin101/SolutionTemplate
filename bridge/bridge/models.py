"""Value objects passed between capture sources, the spool, and the uploader."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class CapturedImage:
    """One frame that came off a camera, however it got here."""

    filename: str
    data: bytes
    source: str
    captured_at: str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    # Where it came from (file path, MTP object id, ...). Used for logs and for a
    # source to remember what it has already seen - never sent upstream.
    origin: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceStatus:
    """What the tray icon and the chair-side UI show about one capture source."""

    name: str
    available: bool
    detail: str
    captures: int = 0
    device: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
            "captures": self.captures,
            "device": self.device,
        }
