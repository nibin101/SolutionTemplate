"""Charting a photograph - the one code path every image travels.

A photo can reach the chart two ways: pushed by the camera bridge, or uploaded
by hand from the chair-side view. Both land here, so deduplication, EXIF
handling, storage layout, session routing and the audit trail behave identically
whichever door the image came through. A manual upload is not a second, weaker
pipeline; it is the same pipeline with the patient already known.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .db import audit, session as db_session, utcnow
from .events import broker
from .quality import assess_image
from .serializers import image_to_dict
from .storage import folder_for, sha256_hex, store_image

# Matches the bridge's own tolerance: camera clocks drift, so a shot seconds
# outside the session window is still this patient's.
CLOCK_SKEW = timedelta(seconds=90)

# How long after a session closes we still accept a late arrival that falls
# *outside* its window, when quarantine is switched off (see
# QUARANTINE_UNASSIGNED in .env.example).
GRACE_PERIOD = timedelta(minutes=10)

MAX_IMAGE_BYTES = 80 * 1024 * 1024


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_session(conn: sqlite3.Connection, operatory: str,
                    captured_at: str | None) -> sqlite3.Row | None:
    """The session whose window contains this photograph, or None to quarantine.

    The rule is the same one the bridge filters by, applied again here because
    the server is the side that must be right: a photograph belongs to the
    patient who was in this chair *when it was taken*. Matching a timestamp to a
    session window is not a guess, so it holds whether the session is still open
    or has just closed - a shot taken a second before Stop, and read off the
    camera a second after, still goes where it belongs.
    """
    reference = _parse_iso(captured_at) or datetime.now(timezone.utc)

    open_row = conn.execute(
        "SELECT * FROM capture_sessions WHERE operatory=? AND status='open'"
        " ORDER BY started_at DESC LIMIT 1",
        (operatory,),
    ).fetchone()
    if open_row is not None:
        started = _parse_iso(open_row["started_at"])
        if started is None or reference >= started - CLOCK_SKEW:
            return open_row
        # Taken before this patient sat down - it is the previous one's, or
        # nobody's. Fall through rather than chart it here.

    recent = conn.execute(
        "SELECT * FROM capture_sessions WHERE operatory=? AND status='closed'"
        " AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
        (operatory,),
    ).fetchone()
    if recent is None:
        return None

    started, ended = _parse_iso(recent["started_at"]), _parse_iso(recent["ended_at"])
    if started is None or ended is None:
        return None

    if started - CLOCK_SKEW <= reference <= ended + CLOCK_SKEW:
        return recent

    # Outside the window entirely. Attaching it now would be a guess, which is
    # exactly what quarantine exists to prevent.
    if settings.quarantine_unassigned:
        return None
    if timedelta(0) <= reference - ended <= GRACE_PERIOD:
        return recent
    return None


def chart_capture(
    data: bytes,
    filename: str | None,
    content_type: str | None,
    *,
    source: str,
    actor: str,
    operatory: str | None = None,
    captured_at: str | None = None,
    camera_make: str | None = None,
    camera_model: str | None = None,
    patient_id: str | None = None,
    expected_hash: str | None = None,
    view: str | None = None,
) -> dict[str, Any]:
    """Store one photograph and attach it to a patient, if we can tell which.

    `patient_id` is set for a manual upload, where the clinician has already
    said who it belongs to. Left None, the patient is derived from the capture
    session open in `operatory` - and if there is none, the image is quarantined
    rather than guessed onto a chart.

    `view` is the shot's own label when the source knows it (the simulator
    tags each frame - "Frontal retracted", "Right buccal" - the standard dental
    photography series), used by the quality gate to decide whether a face is
    expected; a real camera filename carries no such thing, so it falls back to
    filename keywords when `view` is absent.
    """
    if not data:
        return {"status": "rejected", "reason": "empty file"}
    if len(data) > MAX_IMAGE_BYTES:
        return {"status": "rejected", "reason": "file larger than 80 MB"}

    digest = sha256_hex(data)
    if expected_hash and expected_hash != digest:
        # The bridge hashes before sending; a mismatch means the bytes changed
        # in transit, so we refuse rather than chart a corrupt image.
        return {"status": "rejected", "reason": "hash mismatch"}

    filename = filename or f"{digest}.jpg"

    with db_session() as conn:
        patient = None
        session_id = None

        existing = conn.execute(
            "SELECT * FROM images WHERE content_hash=?", (digest,)
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", "image": image_to_dict(existing)}

        # Quality gate: blur and (for extraoral views) a visible face. Runs
        # before storage or patient resolution - a rejected image is never
        # written to disk, never charted, and never quarantined for review;
        # it was simply not a usable clinical photograph.
        if settings.quality.enabled:
            quality = assess_image(data, filename, settings.quality, view)
            if not quality.passed:
                reason = "; ".join(quality.reasons)
                audit(conn, actor=actor, action="ingest.rejected", detail=reason)
                return {
                    "status": "rejected",
                    "reason": reason,
                    "quality": {
                        "blur_score": quality.blur_score,
                        "faces_detected": quality.faces_detected,
                    },
                }

        if patient_id:
            patient = conn.execute(
                "SELECT * FROM patients WHERE id=?", (patient_id,)
            ).fetchone()
            if patient is None:
                return {"status": "rejected", "reason": "unknown patient"}
        else:
            target = resolve_session(conn, operatory or "OP-1", captured_at)
            if target is not None:
                session_id = target["id"]
                patient = conn.execute(
                    "SELECT * FROM patients WHERE id=?", (target["patient_id"],)
                ).fetchone()

        # Stored once, in the folder it belongs in - no move on the happy path.
        stored = store_image(
            data,
            filename,
            content_type or "image/jpeg",
            folder_for(patient, captured_at),
        )

        # The camera's own EXIF timestamp is the most trustworthy capture time;
        # the value the bridge reports is the file's mtime, which is a fallback.
        effective_captured_at = stored.captured_at or captured_at or utcnow()

        image_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO images (
                id, content_hash, filename, stored_path, thumb_path, preview_path,
                mime, size_bytes, width, height, captured_at, received_at, source,
                camera_make, camera_model, operatory, session_id, patient_id, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                image_id, digest, stored.stored_path.name,
                str(stored.stored_path),
                str(stored.thumb_path) if stored.thumb_path else None,
                str(stored.preview_path) if stored.preview_path else None,
                stored.mime, stored.size_bytes, stored.width, stored.height,
                effective_captured_at, utcnow(), source,
                stored.camera_make or camera_make,
                stored.camera_model or camera_model,
                operatory,
                session_id,
                patient["id"] if patient is not None else None,
                "assigned" if patient is not None else "unassigned",
            ),
        )
        audit(
            conn, actor=actor, action="ingest", subject=image_id,
            detail=f"operatory={operatory or '-'} session={session_id or 'none'}"
                   f" patient={patient['id'] if patient is not None else 'none'}",
        )

        row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        payload = image_to_dict(row)

    broker.publish("image.captured", payload)
    return {"status": "stored", "image": payload}
