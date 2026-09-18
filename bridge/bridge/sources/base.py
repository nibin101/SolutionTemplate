"""The one interface every camera path implements.

Canon, Nikon and Sony ship incompatible SDKs, and a clinic may just as easily
hand you an SD card or a Wi-Fi drop folder. Rather than branching on brand
everywhere, each transport is a `CaptureSource` that does exactly one thing:
call `emit(CapturedImage)` when a new clinical image exists. Everything
downstream - dedupe, spooling, retry, charting - is identical for all of them.

Adding a brand means adding one file in this package. Nothing else changes.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable

from ..logging_setup import setup_logging
from ..models import CapturedImage, SourceStatus
from ..window import CaptureWindow

Emit = Callable[[CapturedImage], None]


class _Keep:
    """Sentinel: 'this argument was not supplied', as distinct from None."""

    __slots__ = ()


KEEP = _Keep()


class CaptureSource(ABC):
    """A single way of getting images off a camera."""

    #: Short id used in .env (ENABLED_SOURCES) and shown in the UI.
    name: str = "base"
    #: One-line description shown in the status panel.
    description: str = ""

    def __init__(self) -> None:
        self.log = setup_logging().getChild(f"source.{self.name}")
        #: Replaced by the app with the one shared window; a source constructed
        #: on its own (a test, a probe) simply sees no session open.
        self.window = CaptureWindow()
        self.captures = 0
        self._detail = "not started"
        self._available = False
        self._device: str | None = None

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        """Block until `stop_event` is set, calling `emit` for each new image.

        Implementations must return promptly once the event is set, and may
        raise - the supervisor restarts a source that dies.
        """

    def probe(self) -> tuple[bool, str]:
        """Cheap readiness check used before the source is started.

        Returns (usable, human-readable detail). A source that reports False is
        skipped rather than started, and the reason is shown in the UI so a
        missing driver or unplugged camera is visible instead of silent.
        """
        return True, "ready"

    # -- status ------------------------------------------------------------

    def note(self, detail: str, available: bool | None = None,
             device: str | None | _Keep = KEEP) -> None:
        """Update what the tray and the chair-side UI say about this source.

        `device` distinguishes three things, which is why it cannot just default
        to None: omit it to leave the current device name alone, pass a name to
        set it, or pass None to say there is no device attached any more.

        That last case used to be unreachable - None was both "no argument" and
        "clear it", so `note("no camera connected", device=None)` kept whatever
        was last plugged in. The status bar then read "Camera ready - <phone>"
        with nothing connected at all, which is precisely the claim a clinician
        must be able to trust before they start shooting.
        """
        self._detail = detail
        if available is not None:
            self._available = available
        if not isinstance(device, _Keep):
            self._device = device

    def count_capture(self) -> None:
        self.captures += 1

    def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name,
            available=self._available,
            detail=self._detail,
            captures=self.captures,
            device=self._device,
        )
