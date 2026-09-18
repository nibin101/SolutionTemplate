"""USB capture over MTP/PTP, via the Windows Portable Devices (WPD) API.

Why this is the primary path
----------------------------
Canon, Nikon and Sony all ship different, registration-walled SDKs (EDSDK, the
Nikon SDK, Sony's Camera Remote SDK). Writing three integrations means three
sets of native DLLs to license, ship and keep in step with firmware.

But every one of those bodies also speaks PTP over USB, and Windows exposes PTP
devices through one vendor-neutral COM API: Windows Portable Devices. Pointing
at WPD gets Canon, Nikon and Sony from a single implementation, with no vendor
SDK, no licence and no extra driver on the clinic PC. The vendor SDK adapters in
`sources/vendor/` stay available for the things only they can do (remote
shutter, live view), but they are an enhancement, not a dependency.

How it works
------------
Poll the device tree for image objects and pull the ones taken inside the
current capture session over an `IStream`. The session window (see
`bridge/window.py`) is the whole selection rule, which is why plugging in a
phone does not import somebody's camera roll: those photographs were taken
while nobody was in the chair, so they are never read a second time.

Verification status
-------------------
Verified end to end against a real MTP/PTP device over USB: enumeration, device
naming, tree walk, byte transfer and charting. Not yet verified against an
actual DSLR body, which differs mainly in tree layout (a camera exposes only
DCIM) - so the code deliberately makes no assumption about where images live.

Devices vary more than the documentation suggests, and three of those
differences cost real bugs during that testing: counts have to be requested
through an explicit out-pointer or every device reads as absent, a storage root
is a *functional object* rather than a folder, and date properties come back in
whatever shape the vendor felt like. If a particular body still misbehaves,
switch that station to `shell` (same job, Windows Shell instead of raw COM) or
to `folder` with the vendor's own tether utility; all three feed one pipeline.
"""

from __future__ import annotations

import re
import threading
import time
from ctypes import POINTER, c_ulong, c_wchar_p, cast, pointer
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import config
from ..models import CapturedImage
from ..window import SKIP, TAKE
from .base import CaptureSource, Emit

# WPD property keys: (format GUID, property id). Values come from PortableDevice.h.
WPD_OBJECT_PROPERTIES_V1 = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"
WPD_RESOURCE_DEFAULT_GUID = "{E81E79BE-34F0-41BF-B53F-F1A06AE87842}"

PID_OBJECT_NAME = 4
PID_OBJECT_FORMAT = 6
PID_OBJECT_CONTENT_TYPE = 7
PID_OBJECT_SIZE = 11
PID_OBJECT_ORIGINAL_FILE_NAME = 12
PID_OBJECT_DATE_CREATED = 18
PID_OBJECT_DATE_MODIFIED = 19

WPD_CONTENT_TYPE_IMAGE = "{EF2107D5-A52A-4243-A26B-62D4176D7603}"
WPD_CONTENT_TYPE_FOLDER = "{27E2E392-A111-48E0-AB0C-E17705A05F85}"
# A device's storage root is a "functional object", not a folder - miss this and
# the walk stops at the top of the tree without ever reaching DCIM.
WPD_CONTENT_TYPE_FUNCTIONAL_OBJECT = "{99ED0160-17FF-4C44-9D98-1D7A6F941921}"
CONTAINER_CONTENT_TYPES = {
    WPD_CONTENT_TYPE_FOLDER,
    WPD_CONTENT_TYPE_FUNCTIONAL_OBJECT,
    None,  # unknown type: descend rather than risk missing a vendor layout
}

# Device-level properties live under their own format GUID.
WPD_DEVICE_PROPERTIES_V1 = "{26D4979A-E643-4626-9E2B-736DC0C92FDC}"
PID_DEVICE_MANUFACTURER = 7
PID_DEVICE_MODEL = 8
PID_DEVICE_FRIENDLY_NAME = 12

# Depth guard: DCIM/100CANON/IMG_0001.JPG is three levels; ten is ample.
MAX_WALK_DEPTH = 10

STGM_READ = 0
READ_BLOCK = 256 * 1024

# Guard against a pathological card: no single folder should hold more than this.
MAX_OBJECTS_PER_NODE = 5000

# A safety floor, not a target. MTP enumeration is chatty - each sweep opens
# the device, walks its tree and closes it again - so there is a rate past
# which polling costs more than it gains. This used to sit at 4.0, which is
# most of the time between pressing the shutter and seeing the photograph on
# the chart, and it silently overrode a lower POLL_INTERVAL_SECONDS: a station
# configured for 3s actually ran at 4s and nothing said so.
#
# The device is now held open across sweeps and announces new files through a
# WPD event, so a sweep is just an enumeration and the poll is only a safety
# net for devices that raise nothing. 0.25 keeps a silent device responsive
# without hammering a chatty one.
MIN_POLL_SECONDS = 0.25

# Once a device is raising events, polling it hard is pure waste: the event is
# what delivers the photograph promptly, and the poll is only there in case the
# device goes quiet. Walking a phone's tree twice a second cost 53% of a core
# on the test machine for nothing, so a device with a live subscription gets
# swept on this much slower cadence instead.
#
# Walking a phone's whole tree costs around a second of CPU, so this interval
# is most of the idle cost of the source: 6s measured 16.6% of a core, 20s
# brings it near 5%. It is insurance, not the mechanism - with events live a
# photograph is picked up the moment it is written, and this only bounds how
# long a *missed* event could hide one. A clinic PC running all day should not
# spend a sixth of a core asking a phone a question it already answers.
EVENT_BACKSTOP_SECONDS = 20.0

# Photographs land in one or two folders - DCIM/Camera on a phone, DCIM/100CANON
# on a body - but a phone's tree also holds hundreds of folders belonging to
# every app on it. Enumerating all of them is what a sweep actually costs (the
# per-folder EnumObjects call, not the property reads), so once a folder has
# produced an image the fast sweep looks only there. The whole tree is still
# re-walked this often, to notice a new card, a new DCIM sub-folder, or a
# camera that starts writing somewhere else.
FULL_WALK_SECONDS = 20.0

# Until a photograph has shown which folder to watch there is nothing to target,
# so every sweep has to be a full walk. Doing that at the configured interval
# would burn a fifth of a core before the first shot is even taken, so full
# walks are spaced at least this far apart while bootstrapping. It also bounds
# how long the very first photograph of a session can take to appear.
BOOTSTRAP_WALK_SECONDS = 2.0

# Folders worth watching before anything has been shot from them. Every camera
# and phone writes into DCIM, then into a sub-folder named by the DCF standard
# (100CANON, 100NIKON, 100ANDRO) or, on a phone, simply "Camera". Recognising
# those on the first walk means the cheap targeted sweep is available from the
# start, instead of only after the first photograph has taught it where to
# look - which is the shot the clinician is most likely to be watching for.
_DCF_FOLDER = re.compile(r"^\d{3}[A-Za-z0-9_]{1,5}$")
_PHOTO_FOLDER_NAMES = {"dcim", "camera", "photo", "photos", "images"}


def _looks_like_a_photo_folder(name: str) -> bool:
    cleaned = (name or "").strip()
    return cleaned.lower() in _PHOTO_FOLDER_NAMES or bool(_DCF_FOLDER.match(cleaned))


@dataclass
class _OpenDevice:
    """One camera held open across sweeps, plus its event subscription."""

    device: object
    content: object
    properties: object
    name: str
    #: Kept alive deliberately; COM holds a raw pointer to it.
    sink: object | None = None
    cookie: object | None = None
    #: monotonic timestamp of the last full tree walk.
    last_full: float = 0.0


class WpdUnavailable(RuntimeError):
    """Raised when the WPD COM API cannot be reached on this machine."""


def _load_wpd():
    """Import the generated COM wrappers, building them on first use."""
    try:
        import comtypes  # noqa: F401
        import comtypes.client as cc
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise WpdUnavailable("comtypes is not installed") from exc

    try:
        cc.GetModule("portabledeviceapi.dll")
        cc.GetModule("portabledevicetypes.dll")
        from comtypes.gen import PortableDeviceApiLib as api
        from comtypes.gen import PortableDeviceTypesLib as types
    except Exception as exc:  # pragma: no cover - non-Windows or broken WPD
        raise WpdUnavailable(f"Windows Portable Devices unavailable: {exc}") from exc

    return cc, api, types


def _key(api, guid: str, pid: int):
    """Build a PROPERTYKEY, the (GUID, id) pair WPD identifies properties by."""
    from comtypes import GUID

    property_key = api._tagpropertykey()
    property_key.fmtid = GUID(guid)
    property_key.pid = pid
    return property_key


def _guid_str(value) -> str:
    return str(value).upper()


def _count_out():
    """A real out-pointer for WPD's 'how many?' parameters.

    Passing a plain 0 for these looks like it works - the call succeeds and
    returns 0 - but comtypes never hands WPD somewhere to write the count, so a
    connected camera reads as no camera at all. Always pass a pointer.
    """
    return pointer(c_ulong(0))


def _deref(value) -> int:
    """Read a count back, whether it arrives as a pointer or a plain int."""
    try:
        return int(value.contents.value)
    except AttributeError:
        return int(value)


def _short_pnp(pnp_id: str) -> str:
    """Last-resort label: the readable middle of a PnP id, not the whole thing."""
    parts = [p for p in pnp_id.strip("\\?").split("#") if p and not p.startswith("{")]
    return parts[0] if parts else pnp_id


def _first_string(value) -> str | None:
    """Read the first element out of an out-parameter comtypes handed back.

    Depending on the interface it can arrive as a plain string, a pointer to
    one, or a one-element array, so all three shapes are accepted.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    for extract in (lambda v: v[0], lambda v: v.contents.value, lambda v: v.value):
        try:
            result = extract(value)
        except Exception:
            continue
        if isinstance(result, str):
            return result
    return None


# Date formats seen in the wild. Devices are inconsistent here: a phone under
# test reported no DATE_CREATED at all and a DATE_MODIFIED string of
# "2024/12/06:20:33:30.000", while the WPD docs describe an OLE float. All of
# them have to be accepted, because getting this wrong means either importing a
# whole card or skipping a shot the clinician is waiting for.
_DATE_PATTERNS = (
    "%Y/%m/%d:%H:%M:%S.%f",   # MTP string form
    "%Y/%m/%d:%H:%M:%S",
    "%Y%m%dT%H%M%S",          # PTP standard form
    "%Y-%m-%d %H:%M:%S",
)


def _wpd_date_to_iso(raw) -> str | None:
    """Normalise whatever a device calls a timestamp into ISO-8601 UTC."""
    if raw is None:
        return None

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # OLE date: days since 1899-12-30.
        if raw <= 0:
            return None
        base = datetime(1899, 12, 30, tzinfo=timezone.utc)
        return (base + timedelta(days=float(raw))).replace(microsecond=0).isoformat()

    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()

    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        # Device clocks are local and carry no zone, same as a camera's own.
        return parsed.astimezone().astimezone(timezone.utc).replace(microsecond=0).isoformat()

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class WpdPtpSource(CaptureSource):
    name = "wpd"
    description = "USB tethering over MTP/PTP (Canon, Nikon, Sony) via Windows"

    def __init__(self) -> None:
        super().__init__()
        self._cc = None
        self._api = None
        self._types = None
        # Object ids settled for good, per device: either transferred, or shown
        # to fall outside a session. Anything in here is never read again,
        # which is what keeps watching a phone full of personal photos cheap.
        self._known: dict[str, set[str]] = {}
        #: Object ids known to be folders/storage roots, per device. A folder's
        #: *contents* change, which is why it can never go in `_known` - but its
        #: type does not, and re-reading that over COM for every folder on the
        #: phone on every sweep was most of the cost of a sweep.
        self._containers: dict[str, set[str]] = {}
        #: Folder ids that have actually yielded a photograph, per device. The
        #: fast sweep looks only at these; see FULL_WALK_SECONDS.
        self._hot: dict[str, set[str]] = {}
        #: Cameras held open across sweeps - see `_acquire`.
        self._devices: dict[str, _OpenDevice] = {}
        #: Set by the device's own event callback the instant it writes a file,
        #: which is what lets the loop react rather than wait out the poll.
        self._wake = threading.Event()
        #: Events actually *received*, not merely subscribed to. Advise()
        #: succeeding proves only that the device accepted the subscription -
        #: plenty accept it and then never raise anything - so the fast path is
        #: earned by observation, never assumed.
        self._events_seen = 0

    # -- COM plumbing ------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._api is None:
            self._cc, self._api, self._types = _load_wpd()

    def _manager(self):
        self._ensure_loaded()
        return self._cc.CreateObject(
            self._api.PortableDeviceManager, interface=self._api.IPortableDeviceManager
        )

    def _device_ids(self) -> list[str]:
        """PnP ids of every portable device currently attached.

        WPD uses the usual two-call COM idiom: ask for the count with a null
        buffer, then ask again with a buffer that size. comtypes surfaces the
        in/out parameters as return values, hence the tuple unpacking.
        """
        manager = self._manager()
        # A device attached after this process started is invisible until the
        # manager re-scans, which is every plug-in during a clinic day.
        try:
            manager.RefreshDeviceList()
        except Exception:
            pass

        _, count = manager.GetDevices(POINTER(c_wchar_p)(), _count_out())
        count = _deref(count)
        if count == 0:
            return []

        buffer = (c_wchar_p * count)()
        manager.GetDevices(cast(buffer, POINTER(c_wchar_p)), count)
        return [buffer[i] for i in range(count) if buffer[i]]

    def _read_device_name(self, properties) -> str:
        """Read the camera's own name from its device object.

        `IPortableDeviceManager::GetDeviceFriendlyName` is not usable through
        comtypes (it mistypes the string buffer parameter), and plenty of
        devices leave the friendly name blank anyway - a phone reported an empty
        name but 'Nothing' / 'A142' for manufacturer and model. Reading the
        device object gives all three and reuses the property machinery the
        image walk already depends on.
        """
        api = self._api
        keys = self._cc.CreateObject(
            self._types.PortableDeviceKeyCollection,
            interface=api.IPortableDeviceKeyCollection,
        )
        for pid in (PID_DEVICE_MANUFACTURER, PID_DEVICE_MODEL, PID_DEVICE_FRIENDLY_NAME):
            keys.Add(_key(api, WPD_DEVICE_PROPERTIES_V1, pid))
        values = properties.GetValues("DEVICE", keys)

        def text(pid: int) -> str:
            try:
                value = values.GetStringValue(_key(api, WPD_DEVICE_PROPERTIES_V1, pid))
            except Exception:
                return ""
            return value.strip() if isinstance(value, str) else ""

        friendly = text(PID_DEVICE_FRIENDLY_NAME)
        if friendly:
            return friendly
        make, model = text(PID_DEVICE_MANUFACTURER), text(PID_DEVICE_MODEL)
        return " ".join(part for part in (make, model) if part)

    def _device_label(self, pnp_id: str) -> str:
        """Name for status display; opens the device briefly to ask it."""
        try:
            device = self._open_device(pnp_id)
        except Exception:
            return _short_pnp(pnp_id)
        try:
            return self._read_device_name(device.Content().Properties()) or _short_pnp(pnp_id)
        except Exception:
            return _short_pnp(pnp_id)
        finally:
            try:
                device.Close()
            except Exception:
                pass

    # -- holding the device open -------------------------------------------

    def _acquire(self, pnp_id: str) -> "_OpenDevice":
        """An open handle for this device, reused across sweeps.

        Opening an MTP device is not cheap - it is a USB open plus a session
        negotiation - and doing it once per poll is what forced the poll
        interval to be measured in seconds. Held open, a sweep is just an
        enumeration, so it can run far more often and a photograph reaches the
        chart in a fraction of the time.
        """
        entry = self._devices.get(pnp_id)
        if entry is not None:
            return entry

        device = self._open_device(pnp_id)
        content = device.Content()
        properties = content.Properties()
        name = self._read_device_name(properties) or _short_pnp(pnp_id)
        entry = _OpenDevice(device=device, content=content,
                            properties=properties, name=name)
        self._subscribe(entry)
        self._devices[pnp_id] = entry
        self.log.info("opened %s (events: %s)", name,
                      "yes" if entry.cookie is not None else "polling only")
        return entry

    def _release(self, pnp_id: str) -> None:
        """Let go of a handle - on error, or when the camera is unplugged."""
        entry = self._devices.pop(pnp_id, None)
        if entry is None:
            return
        try:
            if entry.cookie is not None:
                entry.device.Unadvise(entry.cookie)
        except Exception:
            pass
        try:
            entry.device.Close()
        except Exception:
            pass

    def _release_all(self) -> None:
        for pnp_id in list(self._devices):
            self._release(pnp_id)

    def _subscribe(self, entry: "_OpenDevice") -> None:
        """Ask the device to announce new objects instead of being asked.

        This is what removes the poll delay: WPD raises an event the moment the
        camera commits a file, so the sweep runs then rather than up to a whole
        interval later. Strictly an accelerator - devices differ wildly in what
        they actually raise, so a refusal here is logged and ignored and the
        poll below carries on doing the work by itself.
        """
        try:
            import comtypes

            api = self._api
            wake = self._wake

            source = self

            class _ObjectAddedSink(comtypes.COMObject):
                _com_interfaces_ = [api.IPortableDeviceEventCallback]

                def OnEvent(self, _parameters):  # noqa: N802 - COM vtable name
                    source._events_seen += 1
                    if source._events_seen == 1:
                        source.log.info(
                            "device events are live - polling drops to the backstop"
                        )
                    wake.set()
                    return 0

            sink = _ObjectAddedSink()
            parameters = self._cc.CreateObject(
                self._types.PortableDeviceValues, interface=api.IPortableDeviceValues
            )
            # The sink must outlive this call - COM holds a raw pointer, and a
            # collected callback is a crash rather than a missed photograph.
            entry.sink = sink
            entry.cookie = entry.device.Advise(0, sink, parameters)
        except Exception as exc:
            entry.sink = None
            entry.cookie = None
            self.log.info("device events unavailable (%s); polling only", exc)

    def _open_device(self, pnp_id: str):
        values = self._cc.CreateObject(
            self._types.PortableDeviceValues, interface=self._api.IPortableDeviceValues
        )
        device = self._cc.CreateObject(
            self._api.PortableDevice, interface=self._api.IPortableDevice
        )
        device.Open(pnp_id, values)
        return device

    def _enumerate(self, content, parent_id: str) -> list[str]:
        """Child object ids of one node.

        Fetched one at a time on purpose: comtypes allocates the out-parameter
        array itself, and a single element is the shape it gets right for every
        device. Enumeration is cheap next to the transfers it feeds.
        """
        enumerator = content.EnumObjects(0, parent_id, None)
        found: list[str] = []
        while len(found) < MAX_OBJECTS_PER_NODE:
            try:
                object_ids, fetched = enumerator.Next(1, _count_out())
            except Exception as exc:
                self.log.debug("enumeration ended for %s: %s", parent_id, exc)
                break
            if not _deref(fetched):
                break
            value = _first_string(object_ids)
            if value:
                found.append(value)
        return found

    def _properties(self, properties, object_id: str) -> dict[str, object]:
        """Read the handful of properties we need for one object."""
        api = self._api
        keys = self._cc.CreateObject(
            self._types.PortableDeviceKeyCollection,
            interface=api.IPortableDeviceKeyCollection,
        )
        for pid in (PID_OBJECT_NAME, PID_OBJECT_ORIGINAL_FILE_NAME,
                    PID_OBJECT_CONTENT_TYPE, PID_OBJECT_FORMAT,
                    PID_OBJECT_SIZE, PID_OBJECT_DATE_CREATED,
                    PID_OBJECT_DATE_MODIFIED):
            keys.Add(_key(api, WPD_OBJECT_PROPERTIES_V1, pid))

        values = properties.GetValues(object_id, keys)

        def string_value(pid: int) -> str | None:
            try:
                return values.GetStringValue(_key(api, WPD_OBJECT_PROPERTIES_V1, pid))
            except Exception:
                return None

        def guid_value(pid: int) -> str | None:
            try:
                return _guid_str(values.GetGuidValue(_key(api, WPD_OBJECT_PROPERTIES_V1, pid)))
            except Exception:
                return None

        def number_value(pid: int):
            try:
                return values.GetUnsignedLargeIntegerValue(
                    _key(api, WPD_OBJECT_PROPERTIES_V1, pid)
                )
            except Exception:
                return None

        def date_value(pid: int) -> str | None:
            """A date, whichever of the three shapes this device happens to use."""
            for read in (
                lambda k: values.GetStringValue(k),
                lambda k: values.GetFloatValue(k),
            ):
                try:
                    raw = read(_key(api, WPD_OBJECT_PROPERTIES_V1, pid))
                except Exception:
                    continue
                parsed = _wpd_date_to_iso(raw)
                if parsed:
                    return parsed
            return None

        return {
            "name": string_value(PID_OBJECT_NAME),
            "filename": string_value(PID_OBJECT_ORIGINAL_FILE_NAME),
            "content_type": guid_value(PID_OBJECT_CONTENT_TYPE),
            "size": number_value(PID_OBJECT_SIZE),
            # Plenty of devices do not implement DATE_CREATED; modified time is
            # the same instant for a file a camera has only ever written once.
            "captured_at": (date_value(PID_OBJECT_DATE_CREATED)
                            or date_value(PID_OBJECT_DATE_MODIFIED)),
        }

    def _download(self, content, object_id: str) -> bytes | None:
        """Stream one object off the camera."""
        resources = content.Transfer()
        try:
            optimal, stream = resources.GetStream(
                object_id,
                _key(self._api, WPD_RESOURCE_DEFAULT_GUID, 0),
                STGM_READ,
                _count_out(),
            )
        except Exception as exc:
            self.log.warning("cannot open stream for %s: %s", object_id, exc)
            return None

        block = max(_deref(optimal), READ_BLOCK)
        chunks = bytearray()
        try:
            while True:
                buffer, read = stream.RemoteRead(block)
                if not read:
                    break
                chunks.extend(bytearray(buffer)[:read])
        except Exception as exc:
            self.log.warning("transfer of %s failed after %d bytes: %s",
                             object_id, len(chunks), exc)
            return None
        return bytes(chunks)

    # -- CaptureSource -----------------------------------------------------

    def probe(self) -> tuple[bool, str]:
        try:
            self._ensure_loaded()
        except WpdUnavailable as exc:
            return False, str(exc)

        # probe() runs on the supervisor thread, which has no COM apartment of
        # its own yet - without this the very first enumeration fails and the
        # source would be written off as unavailable.
        import comtypes

        comtypes.CoInitialize()
        try:
            devices = self._device_ids()
            if not devices:
                return True, "no camera connected yet - will pick one up when plugged in"
            names = ", ".join(self._device_label(d) for d in devices[:3])
            return True, f"{len(devices)} portable device(s): {names}"
        except Exception as exc:
            return False, f"device enumeration failed: {exc}"
        finally:
            comtypes.CoUninitialize()

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        import comtypes

        # Each worker thread needs its own COM apartment.
        comtypes.CoInitialize()
        interval = max(config.poll_interval, MIN_POLL_SECONDS)
        if config.poll_interval < MIN_POLL_SECONDS:
            # Say so rather than quietly running slower than asked. Silently
            # ignoring a configured value is how someone ends up convinced the
            # setting does nothing.
            self.log.warning(
                "POLL_INTERVAL_SECONDS=%.1f is below the %.1fs floor for MTP; "
                "polling every %.1fs", config.poll_interval, MIN_POLL_SECONDS, interval,
            )
        self.log.info("polling the camera every %.1fs (device events wake it sooner)",
                      interval)
        self.note("scanning for cameras", available=True)
        try:
            while not stop_event.is_set():
                # Cleared before the sweep, never after: an event that arrives
                # while we are already walking the tree must still cause another
                # pass, or the photograph that raised it waits for the poll.
                self._wake.clear()
                try:
                    self._sweep(emit, stop_event)
                except WpdUnavailable as exc:
                    self.note(str(exc), available=False)
                    return
                except Exception as exc:
                    # A camera unplugged mid-transfer throws; log and keep going.
                    self.log.warning("sweep failed: %s", exc)
                    self.note(f"recovering: {exc}", available=True)

                # Whichever comes first: the camera announcing a new file, or
                # the poll falling due. A device that is raising events needs
                # only a slow backstop; one that is silent has to be asked, so
                # it keeps the configured interval.
                if stop_event.wait(0):
                    break
                self._wake.wait(self._quiet_period(interval))
        finally:
            self._release_all()
            comtypes.CoUninitialize()
            self.note("stopped", available=False)

    def _quiet_period(self, interval: float) -> float:
        """How long to wait before sweeping again, absent an event.

        Every open device raising events means nothing is gained by asking -
        the answer arrives on its own - so the sweep drops back to a backstop.
        A single silent device pulls the whole loop back to the configured
        interval, because that one has to be polled to be noticed.
        """
        if not self._devices or not self._events_seen:
            # Nothing has ever announced itself, so asking is the only way to
            # find out. This is the case that matters: a subscription the
            # device accepted and never honours would otherwise leave a
            # photograph sitting on the camera for a whole backstop period.
            return interval
        if all(e.cookie is not None for e in self._devices.values()):
            return max(interval, EVENT_BACKSTOP_SECONDS)
        return interval

    def _sweep(self, emit: Emit, stop_event: threading.Event) -> None:
        device_ids = self._device_ids()
        if not device_ids:
            self.note("no camera connected", available=True, device=None)
            return

        # Cameras that have gone away must not keep a handle (or an event
        # subscription) alive behind them.
        for gone in [p for p in self._devices if p not in device_ids]:
            self.log.info("camera disconnected: %s", self._devices[gone].name)
            self._release(gone)

        for pnp_id in device_ids:
            if stop_event.is_set():
                return
            try:
                entry = self._acquire(pnp_id)
            except Exception as exc:
                self.log.debug("cannot open %s: %s", _short_pnp(pnp_id), exc)
                self._release(pnp_id)
                continue

            try:
                known = self._known.setdefault(pnp_id, set())
                containers = self._containers.setdefault(pnp_id, set())
                hot = self._hot.setdefault(pnp_id, set())
                now = time.monotonic()

                gap = FULL_WALK_SECONDS if hot else BOOTSTRAP_WALK_SECONDS
                if now - entry.last_full >= gap:
                    # The whole tree: finds a new card, a new DCIM sub-folder,
                    # or a camera that has started writing somewhere else.
                    pulled = self._walk(entry.content, entry.properties, "DEVICE",
                                        known, containers, entry.name, emit,
                                        stop_event, hot=hot)
                    entry.last_full = now
                elif hot:
                    # Cheap sweep: one enumeration per folder that has actually
                    # produced a photograph, instead of one per folder on the
                    # device. This is what makes sub-second polling affordable.
                    pulled = 0
                    for folder in list(hot):
                        pulled += self._walk(
                            entry.content, entry.properties, folder, known,
                            containers, entry.name, emit, stop_event,
                            hot=hot, recurse=False,
                        )
                else:
                    # Nothing to target yet and a full walk is not due; the
                    # bootstrap gap above is what bounds the wait.
                    pulled = 0
                self.note(
                    f"{entry.name} - {self.window.describe()}"
                    + (f", {pulled} taken this sweep" if pulled else ""),
                    available=True,
                    device=entry.name,
                )
            except Exception:
                # The handle may be stale (cable pulled mid-walk). Drop it so
                # the next sweep opens a fresh one rather than failing forever.
                self._release(pnp_id)
                raise

    def _walk(self, content, properties, parent_id: str, known: set[str],
              containers: set[str], device_name: str, emit: Emit,
              stop_event: threading.Event, depth: int = 0,
              hot: set[str] | None = None, recurse: bool = True) -> int:
        """Walk the device tree, transferring what the session window claims.

        `known` is the reason this stays cheap on a phone: a file judged once is
        never read again. It is only ever added to for a decision that cannot
        change - transferred, or taken before the current session began. A file
        taken *after* the window closed is deliberately left out of it, because
        the next patient's window may well claim it.
        """
        if depth > MAX_WALK_DEPTH:
            return 0
        pulled = 0
        for object_id in self._enumerate(content, parent_id):
            if stop_event.is_set():
                return pulled
            if object_id in known:
                continue

            if object_id in containers:
                # Already established as a folder; descend without paying for
                # another property read. This is the hot path on a phone, where
                # the great majority of objects walked are folders we have
                # classified on a previous sweep.
                if recurse:
                    pulled += self._walk(content, properties, object_id, known,
                                         containers, device_name, emit, stop_event,
                                         depth + 1, hot, recurse)
                continue

            try:
                info = self._properties(properties, object_id)
            except Exception as exc:
                self.log.debug("cannot read properties of %s: %s", object_id, exc)
                known.add(object_id)
                continue

            content_type = info.get("content_type")
            filename = str(info.get("filename") or info.get("name") or "")
            is_image = content_type == WPD_CONTENT_TYPE_IMAGE or config.is_image(filename)

            if not is_image and content_type in CONTAINER_CONTENT_TYPES:
                # Storage roots, folders and anything of unknown type: descend.
                # A folder id is not marked as seen, because its contents change
                # - but its type is remembered so the next sweep skips this
                # property read entirely.
                containers.add(object_id)
                if hot is not None and _looks_like_a_photo_folder(
                    str(info.get("name") or info.get("filename") or "")
                ):
                    hot.add(object_id)
                if recurse:
                    pulled += self._walk(content, properties, object_id, known,
                                         containers, device_name, emit, stop_event,
                                         depth + 1, hot, recurse)
                continue

            if not is_image:
                # A known non-image (audio, video, document): skip it for good.
                known.add(object_id)
                continue

            captured_at = info.get("captured_at")
            captured_at = captured_at if isinstance(captured_at, str) else None

            verdict = self.window.verdict(captured_at)
            if verdict != TAKE:
                if verdict == SKIP:
                    known.add(object_id)
                continue

            data = self._download(content, object_id)
            known.add(object_id)
            if not data:
                continue

            emit(
                CapturedImage(
                    filename=filename or f"{object_id}.jpg",
                    data=data,
                    source=self.name,
                    captured_at=captured_at,
                    camera_model=device_name,
                    origin=f"wpd://{object_id}",
                )
            )
            self.count_capture()
            # This folder produces photographs, so the fast sweep should look
            # here rather than re-walking the whole device.
            if hot is not None:
                hot.add(parent_id)
            pulled += 1
        return pulled
