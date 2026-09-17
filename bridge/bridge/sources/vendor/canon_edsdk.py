"""Canon EDSDK adapter - true event-driven tethering for Canon bodies.

What this buys over the generic `wpd` source
--------------------------------------------
WPD polls: a shot appears in the chart a second or two after it is taken. EDSDK
pushes: the camera raises `kEdsObjectEvent_DirItemRequestTransfer` the instant
the shutter closes and we stream the file straight out of the body, without it
ever being written to the card. For a five-shot clinical series that is the
difference between "it shows up" and "it is already there".

Requirements
------------
Canon does not redistribute EDSDK. Download it from Canon's developer programme,
then point `CANON_EDSDK_DLL` in bridge/.env at `EDSDK.dll` (use the build whose
bitness matches your Python - a 64-bit Python needs the 64-bit EDSDK).

Verification status: the adapter is written against EDSDK 13.x headers and is
exercised only through its unavailable-path in the test suite, because the DLL
cannot be redistributed with this repo. With no DLL configured the source
reports itself unavailable and the bridge runs on `wpd` instead - Canon capture
still works, it just polls rather than pushes.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, byref, c_char, c_int, c_uint32, c_uint64, c_void_p

from ...config import config
from ...models import CapturedImage, utcnow_iso
from ..base import CaptureSource, Emit

EDS_ERR_OK = 0

# Event ids and property ids from EDSDKTypes.h / EDSDK.h.
kEdsObjectEvent_DirItemRequestTransfer = 0x00000208
kEdsPropID_SaveTo = 0x0000000B
kEdsSaveTo_Host = 2

# Camera "capacity" the host claims to have. Canon bodies refuse to transfer
# unless the host advertises free space; these are the values Canon's own
# samples use for "plenty of room".
HOST_CAPACITY_CLUSTERS = 0x7FFFFFFF
HOST_CAPACITY_BYTES_PER_SECTOR = 512


class EdsDirectoryItemInfo(ctypes.Structure):
    _fields_ = [
        ("size", c_uint64),
        ("isFolder", c_int),
        ("groupID", c_uint32),
        ("option", c_uint32),
        ("szFileName", c_char * 256),
        ("format", c_uint32),
        ("dateTime", c_uint32),
    ]


class EdsCapacity(ctypes.Structure):
    _fields_ = [
        ("numberOfFreeClusters", c_int),
        ("bytesPerSector", c_int),
        ("reset", c_int),
    ]


# EdsError EDSCALLBACK handler(EdsObjectEvent, EdsBaseRef, EdsVoid* context)
OBJECT_EVENT_HANDLER = ctypes.WINFUNCTYPE(c_uint32, c_uint32, c_void_p, c_void_p)


class CanonEdsdkSource(CaptureSource):
    name = "canon"
    description = "Canon EDSDK push tethering (requires CANON_EDSDK_DLL)"

    def __init__(self) -> None:
        super().__init__()
        self._dll = None
        self._emit: Emit | None = None
        # The callback object must outlive the C call that registered it, or
        # Python will collect it and the camera will fire into freed memory.
        self._handler_ref: OBJECT_EVENT_HANDLER | None = None

    # -- loading -----------------------------------------------------------

    def _load(self):
        if self._dll is not None:
            return self._dll
        if not config.canon_edsdk_dll:
            raise RuntimeError("CANON_EDSDK_DLL is not set in bridge/.env")
        try:
            self._dll = ctypes.WinDLL(config.canon_edsdk_dll)
        except OSError as exc:
            raise RuntimeError(f"cannot load EDSDK from {config.canon_edsdk_dll}: {exc}") from exc
        try:
            _declare_signatures(self._dll)
        except AttributeError as exc:
            # An EDSDK build that is missing a symbol we declare is the wrong
            # major version; say so now rather than crashing mid-shoot.
            self._dll = None
            raise RuntimeError(f"unexpected EDSDK build: {exc}") from exc
        return self._dll

    def probe(self) -> tuple[bool, str]:
        try:
            self._load()
        except RuntimeError as exc:
            return False, str(exc)
        return True, "EDSDK loaded - waiting for a Canon body"

    # -- SDK helpers -------------------------------------------------------

    @staticmethod
    def _check(code: int, what: str) -> None:
        if code != EDS_ERR_OK:
            raise RuntimeError(f"{what} failed (EDSDK error 0x{code:08X})")

    def _first_camera(self, eds) -> c_void_p:
        camera_list = c_void_p()
        self._check(eds.EdsGetCameraList(byref(camera_list)), "EdsGetCameraList")
        try:
            count = c_uint32(0)
            self._check(eds.EdsGetChildCount(camera_list, byref(count)), "EdsGetChildCount")
            if count.value == 0:
                raise RuntimeError("no Canon camera connected")
            camera = c_void_p()
            self._check(
                eds.EdsGetChildAtIndex(camera_list, 0, byref(camera)), "EdsGetChildAtIndex"
            )
            return camera
        finally:
            eds.EdsRelease(camera_list)

    def _configure_host_transfer(self, eds, camera: c_void_p) -> None:
        """Tell the body to hand files to us instead of writing them to the card."""
        save_to = c_uint32(kEdsSaveTo_Host)
        self._check(
            eds.EdsSetPropertyData(camera, kEdsPropID_SaveTo, 0,
                                   ctypes.sizeof(save_to), byref(save_to)),
            "EdsSetPropertyData(SaveTo=Host)",
        )
        capacity = EdsCapacity(HOST_CAPACITY_CLUSTERS, HOST_CAPACITY_BYTES_PER_SECTOR, 1)
        self._check(eds.EdsSetCapacity(camera, capacity), "EdsSetCapacity")

    def _download(self, eds, directory_item: c_void_p) -> tuple[str, bytes] | None:
        info = EdsDirectoryItemInfo()
        self._check(
            eds.EdsGetDirectoryItemInfo(directory_item, byref(info)),
            "EdsGetDirectoryItemInfo",
        )
        filename = info.szFileName.decode("ascii", errors="replace")

        stream = c_void_p()
        self._check(
            eds.EdsCreateMemoryStream(c_uint64(info.size), byref(stream)),
            "EdsCreateMemoryStream",
        )
        try:
            self._check(
                eds.EdsDownload(directory_item, c_uint64(info.size), stream), "EdsDownload"
            )
            self._check(eds.EdsDownloadComplete(directory_item), "EdsDownloadComplete")

            pointer = c_void_p()
            self._check(eds.EdsGetPointer(stream, byref(pointer)), "EdsGetPointer")
            length = c_uint64(0)
            self._check(eds.EdsGetLength(stream, byref(length)), "EdsGetLength")
            data = ctypes.string_at(pointer, int(length.value))
        finally:
            eds.EdsRelease(stream)
        return filename, data

    # -- CaptureSource -----------------------------------------------------

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        eds = self._load()
        self._emit = emit

        self._check(eds.EdsInitializeSDK(), "EdsInitializeSDK")
        camera = None
        try:
            camera = self._first_camera(eds)
            self._check(eds.EdsOpenSession(camera), "EdsOpenSession")
            self._configure_host_transfer(eds, camera)

            def on_object_event(event: int, ref: int, _context) -> int:
                if event == kEdsObjectEvent_DirItemRequestTransfer:
                    self._handle_transfer(eds, c_void_p(ref))
                elif ref:
                    eds.EdsRelease(c_void_p(ref))
                return EDS_ERR_OK

            self._handler_ref = OBJECT_EVENT_HANDLER(on_object_event)
            self._check(
                eds.EdsSetObjectEventHandler(camera, kEdsObjectEvent_DirItemRequestTransfer,
                                             self._handler_ref, None),
                "EdsSetObjectEventHandler",
            )
            self.note("connected - waiting for the shutter", available=True, device="Canon")
            self.log.info("Canon EDSDK session open")

            # EDSDK delivers events on the thread that pumps them, so this loop
            # is the camera's event loop and must keep running.
            while not stop_event.is_set():
                eds.EdsGetEvent()
                stop_event.wait(0.2)
        finally:
            if camera is not None:
                try:
                    eds.EdsCloseSession(camera)
                    eds.EdsRelease(camera)
                except Exception:
                    pass
            try:
                eds.EdsTerminateSDK()
            except Exception:
                pass
            self._handler_ref = None
            self.note("stopped", available=False)

    def _handle_transfer(self, eds, directory_item: c_void_p) -> None:
        try:
            result = self._download(eds, directory_item)
        except RuntimeError as exc:
            self.log.error("Canon transfer failed: %s", exc)
            return
        finally:
            try:
                eds.EdsRelease(directory_item)
            except Exception:
                pass

        if result is None or self._emit is None:
            return
        filename, data = result
        self._emit(
            CapturedImage(
                filename=filename,
                data=data,
                source=self.name,
                captured_at=utcnow_iso(),
                camera_make="Canon",
                origin=f"edsdk://{filename}",
            )
        )
        self.count_capture()
        self.note(f"last capture: {filename}", available=True, device="Canon")


# EDSDK returns 32-bit error codes and takes pointer-sized handles; declaring the
# ones we use keeps ctypes from truncating handles on 64-bit Python.
def _declare_signatures(dll) -> None:  # pragma: no cover - needs the real DLL
    dll.EdsGetCameraList.argtypes = [POINTER(c_void_p)]
    dll.EdsGetChildCount.argtypes = [c_void_p, POINTER(c_uint32)]
    dll.EdsGetChildAtIndex.argtypes = [c_void_p, c_int, POINTER(c_void_p)]
    dll.EdsOpenSession.argtypes = [c_void_p]
    dll.EdsCloseSession.argtypes = [c_void_p]
    dll.EdsRelease.argtypes = [c_void_p]
    dll.EdsGetDirectoryItemInfo.argtypes = [c_void_p, POINTER(EdsDirectoryItemInfo)]
    dll.EdsCreateMemoryStream.argtypes = [c_uint64, POINTER(c_void_p)]
    dll.EdsDownload.argtypes = [c_void_p, c_uint64, c_void_p]
    dll.EdsDownloadComplete.argtypes = [c_void_p]
    dll.EdsGetPointer.argtypes = [c_void_p, POINTER(c_void_p)]
    dll.EdsGetLength.argtypes = [c_void_p, POINTER(c_uint64)]
    dll.EdsSetPropertyData.argtypes = [c_void_p, c_uint32, c_int, c_uint32, c_void_p]
    dll.EdsSetCapacity.argtypes = [c_void_p, EdsCapacity]
    dll.EdsSetObjectEventHandler.argtypes = [c_void_p, c_uint32, OBJECT_EVENT_HANDLER, c_void_p]
