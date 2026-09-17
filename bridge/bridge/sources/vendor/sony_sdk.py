"""Sony Camera Remote SDK adapter - placeholder for push tethering.

Sony image transfer already works through the `wpd` source. The Camera Remote
SDK would add shutter-close push events, live view and remote settings, but it
ships as a C++ library with a callback-class API (`IDeviceCallback`) rather than
a flat C export table, so binding it from Python means building a small C shim
first - not something to fake with guessed ctypes signatures.

When that shim exists, implement `run()` to the contract every other source
follows - call `emit(CapturedImage(...))` per shot - and the rest of the
pipeline is unchanged.

Until then: enable `wpd` (polled transfer, works today) or point `folder` at
Sony Imaging Edge Desktop's output directory.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ...config import config
from ..base import CaptureSource, Emit

GUIDANCE = (
    "Sony transfer is handled by the 'wpd' source; the Camera Remote SDK adds "
    "only remote control and push events, and needs a C++ shim to be callable "
    "from Python."
)


class SonySdkSource(CaptureSource):
    name = "sony"
    description = "Sony Camera Remote SDK (not implemented - use wpd or folder)"

    def probe(self) -> tuple[bool, str]:
        if not config.sony_sdk_dll:
            return False, f"SONY_SDK_DLL is not set. {GUIDANCE}"
        if not Path(config.sony_sdk_dll).exists():
            return False, f"SONY_SDK_DLL points at a missing file: {config.sony_sdk_dll}"
        return False, f"SDK found but the adapter is not implemented. {GUIDANCE}"

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        # Never started: the supervisor skips sources whose probe() is False.
        self.note(GUIDANCE, available=False)
        raise NotImplementedError(GUIDANCE)
