"""The capture window is the whole selection rule, so it gets the most tests.

Every photograph on a camera is judged by one question: was it taken while the
patient was in the chair? Getting that wrong in one direction loses a clinical
photograph; in the other it puts somebody's holiday snap - or the previous
patient's shot - into a dental record. The second is the one that matters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bridge.window import CLOCK_SKEW, SKIP, STALE_AFTER, TAKE, WAIT, CaptureWindow


def at(**delta) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


START = at(minutes=-20)
END = at(minutes=-10)
#: These sessions are in the past, so the bridge has to have been watching
#: before them - anything predating the watch is treated as the camera roll.
WATCHING_SINCE = at(hours=-2)


def watching() -> CaptureWindow:
    """A window belonging to a bridge that has been running for two hours."""
    return CaptureWindow(watching_since=WATCHING_SINCE)


@pytest.fixture()
def closed() -> CaptureWindow:
    """A session that ran from 20 to 10 minutes ago."""
    window = watching()
    window.update({"since": iso(START), "until": iso(END)})
    return window


@pytest.fixture()
def live() -> CaptureWindow:
    """A session opened 20 minutes ago and still running."""
    window = watching()
    window.update({"since": iso(START), "until": None})
    return window


# -- the basic rule --------------------------------------------------------


def test_a_shot_taken_during_the_session_is_transferred(closed):
    assert closed.verdict(iso(at(minutes=-15))) == TAKE


def test_a_shot_taken_before_the_session_goes_to_review(closed):
    """Not this patient's - but it happened while the bridge was watching.

    The bridge no longer decides that such a shot is nobody's and drops it.
    It is transferred, and the server refuses to chart it, so it lands in
    Needs assignment where a clinician can place it in one click. Silently
    discarding it here is how a photograph disappears entirely.
    """
    assert closed.verdict(iso(at(minutes=-40))) == TAKE


def test_the_session_boundaries_are_inclusive(closed):
    assert closed.verdict(iso(START)) == TAKE
    assert closed.verdict(iso(END)) == TAKE


def test_an_open_session_has_no_end(live):
    assert live.verdict(iso(at(minutes=-1))) == TAKE
    # Before this patient sat down: transferred, but for review, not for
    # this chart - the server makes that call.
    assert live.verdict(iso(at(minutes=-40))) == TAKE


# -- the three verdicts are three different things -------------------------


def test_a_shot_after_the_session_is_held_not_discarded(closed):
    """It may belong to the next patient, so it must stay up for judgement.

    Discarding it here is how the first photograph of the next appointment goes
    missing: the file would be marked seen and never looked at again.
    """
    assert closed.verdict(iso(at(seconds=-20))) == WAIT


def test_a_held_shot_is_taken_once_the_next_session_claims_it(closed):
    moment = iso(at(seconds=-20))
    assert closed.verdict(moment) == WAIT

    closed.update({"since": iso(at(minutes=-3)), "until": None})
    assert closed.verdict(moment) == TAKE


def test_a_held_shot_is_not_held_for_ever(closed):
    """The hold is a pause for the next session, not a place to lose things.

    Held indefinitely, a shot taken after Stop reached neither a chart nor the
    review queue - it simply never left the camera.
    """
    stale = at(seconds=-int(STALE_AFTER.total_seconds()) - 30)
    assert closed.verdict(iso(stale)) == TAKE


def test_an_undated_photograph_is_never_transferred(live):
    """We cannot show it belongs to this patient, so we do not claim it does."""
    for value in (None, "", "not a date"):
        assert live.verdict(value) == SKIP


# -- nobody in the chair ---------------------------------------------------


def test_an_old_photograph_is_ignored_when_no_session_is_open():
    """A phone's camera roll. Plugging it in must import none of it."""
    assert watching().verdict(iso(at(hours=-30))) == SKIP


def test_anything_already_on_the_camera_is_left_alone():
    """The guard that keeps a ten-thousand-photo phone cheap to watch.

    Everything predating the bridge is dismissed once and never reconsidered,
    which is what allows an unclaimed *new* shot to be sent for review without
    dragging the whole camera roll with it.
    """
    window = CaptureWindow(watching_since=at(minutes=-1))
    assert window.verdict(iso(at(minutes=-5))) == SKIP


def test_a_photograph_just_taken_is_held_when_no_session_is_open():
    """The window arrives on the heartbeat, so it can be seconds behind.

    A clinician who presses Start and immediately shoots must not lose that
    frame just because the bridge has not been told yet.
    """
    assert watching().verdict(iso(at(seconds=-5))) == WAIT


def test_an_unclaimed_shot_is_sent_for_review_rather_than_dropped():
    """Nobody pressed Start, but the photograph was still taken deliberately.

    This used to be SKIP - silently discarded - so a shot taken with no session
    open reached neither a chart nor the review queue, while the chair-side
    panel advertised "photos that arrived while no session was open".
    """
    stale = at(seconds=-int(STALE_AFTER.total_seconds()) - 30)
    assert watching().verdict(iso(stale)) == TAKE


# -- clock drift -----------------------------------------------------------


def test_a_small_clock_drift_does_not_lose_an_honest_shot(closed):
    """Camera clocks drift; a shot seconds outside the window is still ours."""
    assert closed.verdict(iso(START - CLOCK_SKEW + timedelta(seconds=5))) == TAKE
    assert closed.verdict(iso(END + CLOCK_SKEW - timedelta(seconds=5))) == TAKE


def test_drift_tolerance_cannot_reach_the_next_appointment(closed):
    """Ten minutes out is far past any clock drift.

    It is still transferred - everything photographed while watching is - but
    it is well outside the window, so the server quarantines it rather than
    charting it to this patient. That boundary is asserted in the backend's
    own capture-window tests.
    """
    assert closed.verdict(iso(START - timedelta(minutes=10))) == TAKE
    assert closed.verdict(iso(END + timedelta(minutes=10))) == WAIT


# -- state -----------------------------------------------------------------


def test_the_window_reports_whether_anyone_is_in_the_chair(live, closed):
    assert live.is_open() is True
    assert closed.is_open() is False
    assert CaptureWindow().is_open() is False


def test_closing_the_window_stops_capture_entirely(live):
    moment = iso(at(hours=-30))
    live.update(None)
    assert live.verdict(moment) == SKIP
    assert live.bounds() == (None, None)


def test_an_unchanged_window_is_not_reported_as_a_change(live):
    assert live.update({"since": iso(START), "until": None}) is False
    assert live.update({"since": iso(START), "until": iso(END)}) is True


def test_a_malformed_window_is_treated_as_no_session():
    window = CaptureWindow()
    assert window.update({"since": "yesterday please", "until": None}) is False
    assert window.is_open() is False
