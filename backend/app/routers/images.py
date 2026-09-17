"""Image retrieval, and manual assignment of quarantined captures."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..db import audit, session as db_session
from ..events import broker
from ..schemas import AssignIn
from ..serializers import image_to_dict
from ..storage import folder_for, move_image

router = APIRouter(prefix="/api/images", tags=["images"])


def _load(conn, image_id: str):
    row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Image not found")
    return row


@router.get("/unassigned")
def unassigned_images():
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM images WHERE status='unassigned' ORDER BY received_at DESC"
        ).fetchall()
    return [image_to_dict(r) for r in rows]


@router.get("/recent")
def recent_images(limit: int = 40):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM images ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [image_to_dict(r) for r in rows]


@router.get("/{image_id}/file")
def image_file(image_id: str):
    with db_session() as conn:
        row = _load(conn, image_id)
    path = Path(row["stored_path"])
    if not path.exists():
        raise HTTPException(410, "Image file is no longer on disk")
    return FileResponse(path, media_type=row["mime"], filename=row["filename"])


@router.get("/{image_id}/thumb")
def image_thumb(image_id: str):
    with db_session() as conn:
        row = _load(conn, image_id)
    if not row["thumb_path"]:
        raise HTTPException(404, "No thumbnail for this image")
    path = Path(row["thumb_path"])
    if not path.exists():
        raise HTTPException(410, "Thumbnail is no longer on disk")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/{image_id}/assign")
def assign_image(image_id: str, payload: AssignIn):
    """Attach a quarantined image to a patient, directly or via a session."""
    if not payload.session_id and not payload.patient_id:
        raise HTTPException(400, "Provide session_id or patient_id")

    with db_session() as conn:
        row = _load(conn, image_id)
        previous = row["patient_id"]

        if payload.session_id:
            target = conn.execute(
                "SELECT * FROM capture_sessions WHERE id=?", (payload.session_id,)
            ).fetchone()
            if target is None:
                raise HTTPException(404, "Session not found")
            session_id, patient_id = target["id"], target["patient_id"]
        else:
            patient = conn.execute(
                "SELECT * FROM patients WHERE id=?", (payload.patient_id,)
            ).fetchone()
            if patient is None:
                raise HTTPException(404, "Patient not found")
            session_id, patient_id = None, patient["id"]

        # Re-file the photograph so the folder tree matches the chart. Done
        # before the row is updated: if the move fails we keep pointing at the
        # file that is actually there.
        target_patient = conn.execute(
            "SELECT * FROM patients WHERE id=?", (patient_id,)
        ).fetchone()
        new_path = move_image(
            Path(row["stored_path"]), folder_for(target_patient, row["captured_at"])
        )

        conn.execute(
            "UPDATE images SET session_id=?, patient_id=?, status='assigned',"
            " stored_path=?, filename=? WHERE id=?",
            (session_id, patient_id, str(new_path), new_path.name, image_id),
        )
        audit(conn, actor="ui", action="image.assign", subject=image_id,
              detail=f"from={previous or 'unassigned'} to={patient_id}")
        row = _load(conn, image_id)
        data = image_to_dict(row)

    broker.publish("image.assigned", data)
    return data
