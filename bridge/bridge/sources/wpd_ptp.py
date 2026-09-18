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

# Guard against a pathological card. This has to clear a real phone's camera
# roll with room to spare: a device whose folder hits the cap has its listing
# truncated, and because MTP promises no enumeration order, the photograph just
# taken can be the one that falls off the end and is never charted. The test
# phone was already sitting exactly on the old 5000 limit.
MAX_OBJECTS_PER_NODE = 100000

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
# Deliberately NOT "dcim": DCIM holds *folders*, not photographs. Seeding it
# meant the fast sweep enumerated DCIM every second, saw only its sub-folders,
# and never looked inside DCIM/Camera where the pictures actually are - so new
# shots were found only by the periodic full walk. Match the leaf folders a
# camera really writes into instead.
_PHOTO_FOLDER_NAMES = {"camera", "photo", "photos", "images"}

#: The folder every camera and phone writes real captures into. Both the DCF
#: standard cameras follow and Android put captures here.
DCIM = "dcim"


def _is_hidden(name: str) -> bool:
    """Android caches live in dot-folders and hold no clinical photographs."""
    return (name or "").startswith(".")


def _looks_like_a_photo_folder(name: str) -> bool:
    cleaned = (name or "").strip()
    return cleaned.lower() in _PHOTO_FOLDER_NAMES or bool(_DCF_FOLDER.match(cleaned))


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
        #: When each device last had a full tree walk (monotonic).
        self._last_full: dict[str, float] = {}
        #: Where photographs live on each device - see `_roots`.
        self._roots_cache: dict[str, list[str]] = {}

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

    def _open_device(self, pnp_id: str):
        values = self._cc.CreateObject(
            self._types.PortableDeviceValues, interface=self._api.IPortableDeviceValues
        )
        device = self._cc.CreateObject(
            self._api.PortableDevice, interface=self._api.IPortableDevice
        )
        device.Open(pnp_id, values)
        return device

    def _timed(self, label: str, fn):
        """Run `fn`, logging how long it took. MTP work is I/O bound over USB,
        so wall-clock is the number that matters here, not CPU."""
        started = time.monotonic()
        try:
            return fn()
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 0.2:
                self.log.debug("[timing] %s took %.2fs", label, elapsed)

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
        self.log.info("polling the camera every %.1fs", interval)
        self.note("scanning for cameras", available=True)
        try:
            while not stop_event.is_set():
                try:
                    self._sweep(emit, stop_event)
                except WpdUnavailable as exc:
                    self.note(str(exc), available=False)
                    return
                except Exception as exc:
                    # A camera unplugged mid-transfer throws; log and keep going.
                    self.log.warning("sweep failed: %s", exc)
                    self.note(f"recovering: {exc}", available=True)

                stop_event.wait(interval)
        finally:
            comtypes.CoUninitialize()
            self.note("stopped", available=False)

    def _roots(self, content, properties, pnp_id: str) -> list[str]:
        """Where on this device real photographs live.

        Everything clinical is written to DCIM - that is the DCF standard
        cameras follow and where Android puts captures. Walking the rest of a
        phone is worse than merely slow: `.thumbnails` alone held 8666 JPEGs on
        the test handset, which is both four seconds of enumeration per sweep
        and a pile of 20 KB thumbnails indistinguishable, to this code, from
        clinical photographs. Two of them reached a patient's chart.

        Falls back to the whole device when there is no DCIM, so a body with an
        unusual layout still works - that is the case the original tree walk
        was written for.
        """
        cached = self._roots_cache.get(pnp_id)
        if cached is not None:
            return cached

        found: list[str] = []
        for storage_id in self._enumerate(content, "DEVICE"):
            for child in self._enumerate(content, storage_id):
                try:
                    info = self._properties(properties, child)
                except Exception:
                    continue
                name = str(info.get("name") or info.get("filename") or "")
                if name.lower() == DCIM:
                    found.append(child)

        roots = found or ["DEVICE"]
        self._roots_cache[pnp_id] = roots
        self.log.info(
            "watching %s", "DCIM" if found else "the whole device (no DCIM found)"
        )
        return roots

    def _sweep(self, emit: Emit, stop_event: threading.Event) -> None:
        device_ids = self._device_ids()
        if not device_ids:
            self.note("no camera connected", available=True, device=None)
            return

        for pnp_id in device_ids:
            if stop_event.is_set():
                return
            try:
                device = self._open_device(pnp_id)
            except Exception as exc:
                self.log.debug("cannot open %s: %s", _short_pnp(pnp_id), exc)
                continue

            sweep_started = time.monotonic()
            try:
                content = self._timed("device.Content()", device.Content)
                properties = content.Properties()
                name = self._read_device_name(properties) or _short_pnp(pnp_id)
                known = self._known.setdefault(pnp_id, set())
                containers = self._containers.setdefault(pnp_id, set())
                hot = self._hot.setdefault(pnp_id, set())
                now = time.monotonic()
                last_full = self._last_full.get(pnp_id, 0.0)

                gap = FULL_WALK_SECONDS if hot else BOOTSTRAP_WALK_SECONDS
                if now - last_full >= gap:
                    # Re-walk DCIM: finds a new card or a new sub-folder.
                    pulled = 0
                    for root in self._roots(content, properties, pnp_id):
                        pulled += self._walk(content, properties, root, known,
                                             containers, name, emit, stop_event,
                                             hot=hot)
                    self._last_full[pnp_id] = now
                elif hot:
                    # One enumeration per folder that actually holds
                    # photographs, instead of one per folder on the device.
                    # This is what makes sub-second polling affordable.
                    pulled = 0
                    for folder in list(hot):
                        pulled += self._walk(
                            content, properties, folder, known, containers,
                            name, emit, stop_event, hot=hot, recurse=False,
                        )
                else:
                    pulled = 0

                self.note(
                    f"{name} - {self.window.describe()}"
                    + (f", {pulled} taken this sweep" if pulled else ""),
                    available=True,
                    device=name,
                )
            finally:
                elapsed = time.monotonic() - sweep_started
                if elapsed >= 0.5:
                    self.log.debug("[timing] whole sweep took %.2fs", elapsed)
                try:
                    device.Close()
                except Exception:
                    pass

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
        listed = self._timed(f"enumerate {parent_id[:28]}",
                             lambda: self._enumerate(content, parent_id))
        if len(listed) > 200:
            self.log.debug("[timing] %s holds %d objects", parent_id[:28], len(listed))
        for object_id in listed:
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

            if _is_hidden(str(info.get("name") or "")):
                # `.thumbnails` and friends: caches, never clinical images.
                known.add(object_id)
                continue

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

            data = self._timed(f"download {filename}",
                               lambda: self._download(content, object_id))
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
