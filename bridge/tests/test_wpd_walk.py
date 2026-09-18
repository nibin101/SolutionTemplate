"""Walking a device tree, against a fake camera.

`_walk` is the code that actually runs with a phone plugged in, and it cannot be
exercised on hardware in a test, so the COM surface it touches is faked here:
enumeration, properties and byte transfer. What is being checked is not COM but
the decision - which files come off the device, and, just as important, which
files are written off for good.

The tree is the one a real device gives you: a storage root that is a functional
object rather than a folder, a DCIM tree below it, and a mixture of photographs
from today's session and from the owner's own life.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from bridge.sources import wpd_ptp
from bridge.sources.wpd_ptp import WpdPtpSource
from bridge.window import CaptureWindow

SESSION_START = datetime.now(timezone.utc) - timedelta(minutes=10)


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def ago(**delta) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(**delta))


# The device: object id -> (content type, filename, captured_at, children)
DEVICE = {
    "DEVICE": (wpd_ptp.WPD_CONTENT_TYPE_FUNCTIONAL_OBJECT, "", None, ["storage"]),
    "storage": (wpd_ptp.WPD_CONTENT_TYPE_FUNCTIONAL_OBJECT, "Internal", None, ["dcim"]),
    "dcim": (wpd_ptp.WPD_CONTENT_TYPE_FOLDER, "DCIM", None,
             ["holiday", "in_session", "before", "after", "undated", "notes"]),
    "holiday": (wpd_ptp.WPD_CONTENT_TYPE_IMAGE, "BEACH.JPG", ago(days=200), []),
    "in_session": (wpd_ptp.WPD_CONTENT_TYPE_IMAGE, "IMG_0042.JPG", ago(minutes=5), []),
    "before": (wpd_ptp.WPD_CONTENT_TYPE_IMAGE, "IMG_0041.JPG", ago(minutes=30), []),
    "after": (wpd_ptp.WPD_CONTENT_TYPE_IMAGE, "IMG_0043.JPG", iso(
        datetime.now(timezone.utc) + timedelta(minutes=20)), []),
    "undated": (wpd_ptp.WPD_CONTENT_TYPE_IMAGE, "IMG_0044.JPG", None, []),
    "notes": (None, "notes.txt", ago(minutes=5), []),
}


class FakeContent:
    """Just enough of IPortableDeviceContent for `_walk`."""

    def __init__(self) -> None:
        self.transfers: list[str] = []

    def Properties(self):
        return self


class FakeSource(WpdPtpSource):
    """The real walk, with the three COM calls it makes replaced."""

    def __init__(self) -> None:
        super().__init__()
        self.transfers: list[str] = []

    def _enumerate(self, content, parent_id):
        return DEVICE[parent_id][3]

    def _properties(self, properties, object_id):
        content_type, filename, captured_at, _ = DEVICE[object_id]
        return {"name": filename, "filename": filename,
                "content_type": content_type, "size": 1024,
                "captured_at": captured_at}

    def _download(self, content, object_id):
        self.transfers.append(object_id)
        return b"\xff\xd8\xff" + object_id.encode()


@pytest.fixture()
def source() -> FakeSource:
    """A session that ran from ten minutes ago until two minutes ago."""
    made = FakeSource()
    made.window = CaptureWindow()
    made.window.update({"since": iso(SESSION_START), "until": ago(minutes=2)})
    return made


def walk(source: FakeSource) -> list:
    """One sweep. Returns the images emitted."""
    emitted: list = []
    source._walk(FakeContent(), FakeContent(), "DEVICE",
                 source._known.setdefault("dev", set()),
                 source._containers.setdefault("dev", set()), "Test Camera",
                 emitted.append, threading.Event())
    return emitted


def test_only_the_photograph_taken_during_the_session_comes_across(source):
    emitted = walk(source)

    assert [image.filename for image in emitted] == ["IMG_0042.JPG"]
    assert source.transfers == ["in_session"], "nothing else may be read off the device"


def test_the_owners_own_photographs_are_never_read(source):
    walk(source)
    assert "holiday" not in source.transfers


def test_a_second_sweep_transfers_nothing_new(source):
    walk(source)
    source.transfers.clear()

    assert walk(source) == []
    assert source.transfers == [], "a file judged once is never read again"


def test_a_shot_after_the_window_is_not_written_off(source):
    """It is the next patient's first photograph - it must stay up for judging."""
    walk(source)
    known = source._known["dev"]
    assert "after" not in known
    assert "holiday" in known and "before" in known and "undated" in known


def test_the_next_session_picks_up_the_shot_that_was_waiting(source):
    walk(source)
    source.transfers.clear()

    # The next patient sits down; their session covers that later shot.
    source.window.update({"since": ago(minutes=1), "until": None})
    emitted = walk(source)

    assert [image.filename for image in emitted] == ["IMG_0043.JPG"]


def test_capture_metadata_travels_with_the_image(source):
    image = walk(source)[0]

    assert image.source == "wpd"
    assert image.camera_model == "Test Camera"
    assert image.captured_at == DEVICE["in_session"][2]
    assert image.origin == "wpd://in_session"


def test_nothing_is_taken_while_no_session_is_open():
    """The phone is plugged in and nobody is in the chair."""
    idle = FakeSource()
    assert walk(idle) == []
    assert idle.transfers == []


def test_an_open_session_accepts_a_camera_whose_clock_runs_fast(source):
    """A future timestamp is a misconfigured clock, not a future photograph.

    An open session deliberately has no upper bound. Capping it at "now" would
    mean a body whose clock is set days ahead never transfers anything at all -
    and the shot cannot be re-charted to the next patient later, because once
    transferred it is marked seen.
    """
    source.window.update({"since": iso(SESSION_START), "until": None})
    emitted = walk(source)

    assert "IMG_0043.JPG" in [image.filename for image in emitted]
    assert "holiday" not in source.transfers, "old photographs are still left alone"
