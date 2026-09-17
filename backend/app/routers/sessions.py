"""Capture sessions - the answer to 'which patient is in the chair right now?'.

A session binds one patient to one operatory for a window of time. Opening a
session in an operatory automatically closes any session already open there,
so the invariant 'at most one open session per operatory' cannot be broken by a
clinician forgetting to press Stop.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from ..db import audit, session as db_session, utcnow
from ..events import broker
from ..schemas import SessionIn
from ..serializers import session_to_dict

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_SESSION_SELECT = """
SELECT s.*, (p.first_name || ' ' || p.last_name) AS patient_name,
       (SELECT COUNT(*) FROM images i WHERE i.session_id = s.id) AS image_count
FROM capture_sessions s
JOIN patients p ON p.id = s.patient_id
"""


@router.get("")
def list_sessions(limit: int = 25):
    with db_session() as conn:
        rows = conn.execute(
            _SESSION_SELECT + " ORDER BY s.started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [session_to_dict(r) for r in rows]


@router.get("/active")
def active_session(operatory: str = "OP-1"):
    with db_session() as conn:
        row = conn.execute(
            _SESSION_SELECT + " WHERE s.operatory=? AND s.status='open'"
            " ORDER BY s.started_at DESC LIMIT 1",
            (operatory,),
        ).fetchone()
    return session_to_dict(row) if row else None


@router.post("", status_code=201)
def open_session(payload: SessionIn):
    with db_session() as conn:
        patient = conn.execute(
            "SELECT * FROM patients WHERE id=?", (payload.patient_id,)
        ).fetchone()
        if patient is None:
            raise HTTPException(404, "Patient not found")

        conn.execute(
            "UPDATE capture_sessions SET status='closed', ended_at=?"
            " WHERE operatory=? AND status='open'",
            (utcnow(), payload.operatory),
        )

        session_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO capture_sessions (id, patient_id, operatory, status,"
            " started_at, ended_at, note) VALUES (?,?,?,'open',?,NULL,?)",
            (session_id, payload.patient_id, payload.operatory, utcnow(), payload.note),
        )
        audit(conn, actor="ui", action="session.open", subject=session_id,
              detail=f"patient={payload.patient_id} operatory={payload.operatory}")
        row = conn.execute(_SESSION_SELECT + " WHERE s.id=?", (session_id,)).fetchone()
        data = session_to_dict(row)

    broker.publish("session.opened", data)
    return data


@router.post("/{session_id}/end")
def close_session(session_id: str):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM capture_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Session not found")
        if row["status"] == "open":
            conn.execute(
                "UPDATE capture_sessions SET status='closed', ended_at=? WHERE id=?",
                (utcnow(), session_id),
            )
            audit(conn, actor="ui", action="session.close", subject=session_id)
        row = conn.execute(_SESSION_SELECT + " WHERE s.id=?", (session_id,)).fetchone()
        data = session_to_dict(row)

    broker.publish("session.closed", data)
    return data


@router.get("/{session_id}/images")
def session_images(session_id: str):
    from ..serializers import image_to_dict

    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM images WHERE session_id=? ORDER BY received_at DESC",
            (session_id,),
        ).fetchall()
    return [image_to_dict(r) for r in rows]
