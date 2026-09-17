"""Nikon SDK adapter - placeholder for push tethering on Nikon bodies.

Nikon image transfer already works in this system through the `wpd` source, so
this adapter exists for one reason: the Nikon SDK (`Type0048`/MAID for DSLRs,
the Nikon SDK for Z bodies) is the only way to get shutter-close push events and
remote control, the same gap the Canon adapter fills.

It is deliberately not implemented against guessed symbol names. The Nikon SDK
is distributed under NDA per-body-family, and its entry points differ between
the MAID module interface and the newer SDK, so writing the ctypes bindings
without the headers in front of you produces code that looks finished and cannot
work. When the SDK is in hand, implement `run()` to the same contract every
other source follows - call `emit(CapturedImage(...))` per shot - and nothing
downstream changes.

Until then: enable `wpd` (polled transfer, works today) or point `folder` at
Nikon NX Tether's output directory.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ...config import config
from ..base import CaptureSource, Emit

GUIDANCE = (
    "Nikon transfer is handled by the 'wpd' source; the Nikon SDK adds only "
    "remote control and push events. Set NIKON_SDK_DLL and implement run() to "
    "enable them."
)


class NikonSdkSource(CaptureSource):
    name = "nikon"
    description = "Nikon SDK push tethering (not implemented - use wpd or folder)"

    def probe(self) -> tuple[bool, str]:
        if not config.nikon_sdk_dll:
            return False, f"NIKON_SDK_DLL is not set. {GUIDANCE}"
        if not Path(config.nikon_sdk_dll).exists():
            return False, f"NIKON_SDK_DLL points at a missing file: {config.nikon_sdk_dll}"
        return False, f"SDK found but the adapter is not implemented. {GUIDANCE}"

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        # Never started: the supervisor skips sources whose probe() is False.
        self.note(GUIDANCE, available=False)
        raise NotImplementedError(GUIDANCE)
