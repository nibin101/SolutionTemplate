"""The capture window - the only thing that decides whose photograph this is.

A photograph belongs to a patient because it was taken while that patient was
in the chair. So the bridge never asks "what is new on the camera?"; it asks
"what was taken between Start and Stop?". That is the whole rule.

The backend owns the answer, because it is the only side that knows which
patient is in which operatory, and it sends the window down on every heartbeat
reply. The bridge holds two timestamps and nothing else: patient identity still
never reaches the capture PC.

Three verdicts rather than a boolean, because "no" means two different things:

* SKIP - decided for good. Undated, or taken before this bridge started
  watching. The file is marked seen and never read again, which is what keeps
  a phone holding ten thousand personal photos cheap to watch.
* WAIT - taken moments ago but not yet claimed by a window. The clinician may
  simply not have pressed Start yet, and the heartbeat may not have told us
  about a session that is already open, so it is left unmarked and asked again.
* TAKE - transfer it. Either it falls inside the window, in which case the
  server charts it, or it does not, in which case the server quarantines it
  into Needs assignment.

What is *not* here any more is a verdict that means "drop it silently". A shot
taken after Stop used to sit in WAIT for ever: never transferred, and dropped
for good once the next session started, so it reached neither a chart nor the
review queue. The chair-side panel promises "photos that arrived while no
session was open", and nothing from the camera could ever appear in it.

The rule is now: anything photographed after this bridge began watching is a
deliberate clinical act and reaches the server, which decides whose it is. A
photograph the server cannot place goes to review, where one click assigns it -
which is the recoverable failure this system is built around.
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

    def __init__(self, watching_since: datetime | None = None) -> None:
        self._lock = threading.Lock()
        self._since: datetime | None = None
        self._until: datetime | None = None
        #: When this bridge started watching. Everything already on the camera
        #: at that moment is somebody's camera roll and is never read; anything
        #: photographed afterwards is a deliberate act and reaches the server.
        #: This is what lets an unclaimed shot go to review without the cost of
        #: reconsidering ten thousand old files on every sweep.
        self.watching_since = watching_since or datetime.now(timezone.utc)

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

        now = now or datetime.now(timezone.utc)

        # Already on the camera before this bridge started: somebody's camera
        # roll, and the one case that must stay cheap to dismiss.
        if moment < self.watching_since:
            return SKIP

        if since is None:
            # Nobody in the chair as far as we have been told. A shot from
            # moments ago may simply predate the next heartbeat, so hold it;
            # once it is older than that, send it for review rather than
            # letting it fall through the floor.
            return WAIT if now - moment <= STALE_AFTER else TAKE

        if moment < since - CLOCK_SKEW:
            # Taken before this patient sat down. Not theirs - but it happened
            # while we were watching, so it belongs in review, not nowhere.
            return TAKE
        if until is not None and moment > until + CLOCK_SKEW:
            # After Stop. Give the next session a moment to claim it, then let
            # the server quarantine it.
            return WAIT if now - moment <= STALE_AFTER else TAKE
        return TAKE
