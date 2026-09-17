"""The capture window - the only thing that decides whose photograph this is.

A photograph belongs to a patient because it was taken while that patient was
in the chair. So the bridge never asks "what is new on the camera?"; it asks
"what was taken between Start and Stop?". That is the whole rule.

The backend owns the answer, because it is the only side that knows which
patient is in which operatory, and it sends the window down on every heartbeat
reply. The bridge holds two timestamps and nothing else: patient identity still
never reaches the capture PC.

Three verdicts rather than a boolean, because "no" means two different things:

* SKIP - decided for good. Taken before this session started, or undated. A
  window's start only ever moves forward, so nothing that fails that test will
  ever pass it later; the file can be marked seen and never read again, which
  is what keeps a phone holding ten thousand personal photos cheap to watch.
* WAIT - taken after the window closed. That shot may well belong to the *next*
  patient, so it is left alone, unmarked, to be judged against the next window.
* TAKE - inside the window. Transfer it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

TAKE = "take"
SKIP = "skip"
WAIT = "wait"

# Camera clocks drift, and a body whose clock was never set is often minutes
# out. This slack either side keeps an honest shot from being dropped over a
# clock nobody checked, while staying far too small to reach into the
# neighbouring appointment.
CLOCK_SKEW = timedelta(seconds=90)

# The window is refreshed on the heartbeat, so it can be a few seconds behind
# the clinician: they press Start and take a photograph before the next beat
# arrives. A just-taken photograph is therefore never written off while we
# believe no session is open - we have simply not been told yet. Anything older
# than this cannot be explained that way, and is somebody's camera roll.
STALE_AFTER = timedelta(minutes=2)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clock(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%H:%M:%S") if moment else "-"


class CaptureWindow:
    """Start and end of the capture session, as last reported by the backend.

    Written by the heartbeat thread, read by every capture source, hence the
    lock. It is deliberately tiny: two timestamps and one decision.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since: datetime | None = None
        self._until: datetime | None = None

    # -- state -------------------------------------------------------------

    def update(self, payload: dict | None) -> bool:
        """Apply the window from a heartbeat reply. True if it changed."""
        since = until = None
        if payload:
            since = parse_iso(payload.get("since"))
            until = parse_iso(payload.get("until"))

        with self._lock:
            changed = (since, until) != (self._since, self._until)
            self._since, self._until = since, until
        return changed

    def bounds(self) -> tuple[datetime | None, datetime | None]:
        with self._lock:
            return self._since, self._until

    def is_open(self) -> bool:
        """Is anyone in the chair? (An end time means the session has closed.)"""
        since, until = self.bounds()
        return since is not None and until is None

    def describe(self) -> str:
        since, until = self.bounds()
        if since is None:
            return "no capture session open"
        if until is None:
            return f"capturing since {_clock(since)}"
        return f"session {_clock(since)} to {_clock(until)} (closing)"

    # -- the decision ------------------------------------------------------

    def verdict(self, captured_at: str | None, now: datetime | None = None) -> str:
        """Should this photograph be transferred? TAKE, SKIP or WAIT."""
        since, until = self.bounds()
        moment = parse_iso(captured_at)

        # Undated: we cannot show it was taken while this patient was in the
        # chair, and an unprovable photograph is worse than a missing one.
        if moment is None:
            return SKIP

        if since is None:
            # As far as we know nobody is in the chair. For an old photograph
            # that settles it - this is somebody's camera roll. For one taken
            # moments ago it does not: the session may have opened since the
            # last heartbeat, so hold it and ask again.
            now = now or datetime.now(timezone.utc)
            return WAIT if now - moment <= STALE_AFTER else SKIP

        if moment < since - CLOCK_SKEW:
            return SKIP
        if until is not None and moment > until + CLOCK_SKEW:
            return WAIT
        return TAKE
