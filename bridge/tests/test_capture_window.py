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


@pytest.fixture()
def closed() -> CaptureWindow:
    """A session that ran from 20 to 10 minutes ago."""
    window = CaptureWindow()
    window.update({"since": iso(START), "until": iso(END)})
    return window


@pytest.fixture()
def live() -> CaptureWindow:
    """A session opened 20 minutes ago and still running."""
    window = CaptureWindow()
    window.update({"since": iso(START), "until": None})
    return window


# -- the basic rule --------------------------------------------------------


def test_a_shot_taken_during_the_session_is_transferred(closed):
    assert closed.verdict(iso(at(minutes=-15))) == TAKE


def test_a_shot_taken_before_the_session_is_never_transferred(closed):
    """This is the previous patient's photograph, or nobody's."""
    assert closed.verdict(iso(at(minutes=-40))) == SKIP


def test_the_session_boundaries_are_inclusive(closed):
    assert closed.verdict(iso(START)) == TAKE
    assert closed.verdict(iso(END)) == TAKE


def test_an_open_session_has_no_end(live):
    assert live.verdict(iso(at(minutes=-1))) == TAKE
    assert live.verdict(iso(at(minutes=-40))) == SKIP


# -- the three verdicts are three different things -------------------------


def test_a_shot_after_the_session_is_held_not_discarded(closed):
    """It may belong to the next patient, so it must stay up for judgement.

    Discarding it here is how the first photograph of the next appointment goes
    missing: the file would be marked seen and never looked at again.
    """
    assert closed.verdict(iso(at(minutes=-2))) == WAIT


def test_a_held_shot_is_taken_once_the_next_session_claims_it(closed):
    moment = iso(at(minutes=-2))
    assert closed.verdict(moment) == WAIT

    closed.update({"since": iso(at(minutes=-3)), "until": None})
    assert closed.verdict(moment) == TAKE


def test_an_undated_photograph_is_never_transferred(live):
    """We cannot show it belongs to this patient, so we do not claim it does."""
    for value in (None, "", "not a date"):
        assert live.verdict(value) == SKIP


# -- nobody in the chair ---------------------------------------------------


def test_an_old_photograph_is_ignored_when_no_session_is_open():
    """A phone's camera roll. Plugging it in must import none of it."""
    assert CaptureWindow().verdict(iso(at(hours=-30))) == SKIP


def test_a_photograph_just_taken_is_held_when_no_session_is_open():
    """The window arrives on the heartbeat, so it can be seconds behind.

    A clinician who presses Start and immediately shoots must not lose that
    frame just because the bridge has not been told yet.
    """
    assert CaptureWindow().verdict(iso(at(seconds=-5))) == WAIT


def test_the_hold_expires_so_stray_shots_do_not_queue_up_forever():
    stale = at(seconds=-int(STALE_AFTER.total_seconds()) - 30)
    assert CaptureWindow().verdict(iso(stale)) == SKIP


# -- clock drift -----------------------------------------------------------


def test_a_small_clock_drift_does_not_lose_an_honest_shot(closed):
    """Camera clocks drift; a shot seconds outside the window is still ours."""
    assert closed.verdict(iso(START - CLOCK_SKEW + timedelta(seconds=5))) == TAKE
    assert closed.verdict(iso(END + CLOCK_SKEW - timedelta(seconds=5))) == TAKE


def test_drift_tolerance_cannot_reach_the_next_appointment(closed):
    assert closed.verdict(iso(START - timedelta(minutes=10))) == SKIP
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
