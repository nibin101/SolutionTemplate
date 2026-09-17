"""Bridge liveness, and the capture window the bridge filters by.

Two jobs, both served by the heartbeat:

* upwards - the chair-side UI must be able to say "the camera link is up"
  without anyone walking over to the capture PC, so the bridge posts the state
  of every capture source plus its outbound queue depth;
* downwards - the reply tells the bridge *when* the current capture session
  started and ended. That is all the bridge needs to know which photographs on
  the camera belong on a chart, and it is all it is told: never which patient,
  never even which session.

There is no command channel and nothing for the bridge to listen on. It polls
outward, on the link it already uses, and a clinic PC accepts no connections.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends

from ..db import session as db_session, utcnow
from ..events import broker
from ..security import require_bridge_token

router = APIRouter(prefix="/api/bridge", tags=["bridge"])

# Heartbeats are sent every 10s; three missed beats is a clear signal, and short
# enough that a clinician notices before a whole shoot is lost.
OFFLINE_AFTER = timedelta(seconds=35)

# A session's window stays live to the bridge for this long after Stop is
# pressed. The camera is swept every few seconds, so a shot taken a moment
# before Stop has usually not been read yet; without this tail it would be
# stranded on the card. The window's own end time still bounds what is taken,
# so this cannot pull in anything shot after Stop.
TRAILING_TAIL = timedelta(minutes=5)


def capture_window(conn, operatory: str) -> dict[str, str | None] | None:
    """The time window the bridge should transfer photographs for.

    An open session has no end yet, so the window runs to now. A session that
    has just closed keeps its window - bounded by its own end - for long enough
    that the last sweep still picks up its final shots. Otherwise there is
    nobody in the chair and the answer is None: transfer nothing.
    """
    row = conn.execute(
        "SELECT * FROM capture_sessions WHERE operatory=? AND status='open'"
        " ORDER BY started_at DESC LIMIT 1",
        (operatory,),
    ).fetchone()
    if row is not None:
        return {"since": row["started_at"], "until": None}

    row = conn.execute(
        "SELECT * FROM capture_sessions WHERE operatory=? AND status='closed'"
        " AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
        (operatory,),
    ).fetchone()
    if row is None:
        return None

    ended = datetime.fromisoformat(row["ended_at"])
    if datetime.now(timezone.utc) - ended > TRAILING_TAIL:
        return None
    return {"since": row["started_at"], "until": row["ended_at"]}


@router.post("/heartbeat", dependencies=[Depends(require_bridge_token)])
def heartbeat(payload: dict[str, Any]):
    now = utcnow()
    operatory = str(payload.get("operatory") or "OP-1")

    with db_session() as conn:
        conn.execute(
            "INSERT INTO bridge_status (id, last_seen, payload_json) VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,"
            " payload_json=excluded.payload_json",
            (now, json.dumps(payload)),
        )
        window = capture_window(conn, operatory)

    status = {"online": True, "last_seen": now, **payload}
    broker.publish("bridge.status", status)
    return {"ok": True, "window": window}


@router.get("/status")
def bridge_status():
    with db_session() as conn:
        row = conn.execute("SELECT * FROM bridge_status WHERE id=1").fetchone()
    if row is None:
        return {"online": False, "last_seen": None, "sources": []}

    last_seen = datetime.fromisoformat(row["last_seen"])
    online = datetime.now(timezone.utc) - last_seen <= OFFLINE_AFTER
    return {"online": online, "last_seen": row["last_seen"], **json.loads(row["payload_json"])}
