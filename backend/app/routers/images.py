"""Image retrieval, and manual assignment of quarantined captures."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..db import audit, session as db_session
from ..events import broker
from ..schemas import AssignIn
from ..serializers import image_to_dict
from ..storage import UNASSIGNED_FOLDER, date_folder, folder_for, move_image, patient_folder

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


@router.get("/folders")
def folder_tree():
    """The on-disk photo tree, viewable from the browser instead of Explorer.

    Grouped exactly the way `storage.folder_for` names a folder when it writes
    a file there - by patient (or `_unassigned`) and capture date - so what is
    shown here can never drift from what is actually on disk. Built from the
    database rather than a filesystem walk for the same reason `/recent`
    already is: every entry then carries a real id, so its thumbnail, preview
    and lightbox all work with no special-casing.
    """
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT i.*, p.chart_number, p.first_name, p.last_name
            FROM images i LEFT JOIN patients p ON p.id = i.patient_id
            ORDER BY i.captured_at DESC, i.received_at DESC
            """
        ).fetchall()

    folders: dict[str, dict] = {}
    for row in rows:
        if row["patient_id"]:
            name = patient_folder(row["chart_number"], row["first_name"], row["last_name"])
            display_name = f"{row['first_name']} {row['last_name']} ({row['chart_number']})"
            sort_key = (row["last_name"] or "", row["first_name"] or "")
        else:
            name = UNASSIGNED_FOLDER
            display_name = "Needs assignment"
            sort_key = None  # pinned last, regardless of name

        folder = folders.setdefault(
            name,
            {"name": name, "patient_id": row["patient_id"],
             "display_name": display_name, "sort_key": sort_key, "dates": {}},
        )
        day = date_folder(row["captured_at"])
        bucket = folder["dates"].setdefault(day, {"date": day, "images": []})
        bucket["images"].append(image_to_dict(row))

    ordered = sorted(
        folders.values(), key=lambda f: (f["sort_key"] is None, f["sort_key"] or ("", ""))
    )
    return [
        {
            "name": f["name"],
            "patient_id": f["patient_id"],
            "display_name": f["display_name"],
            "image_count": sum(len(d["images"]) for d in f["dates"].values()),
            "dates": sorted(f["dates"].values(), key=lambda d: d["date"], reverse=True),
        }
        for f in ordered
    ]


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


@router.get("/{image_id}/preview")
def image_preview(image_id: str):
    """Mid-size JPEG for the lightbox.

    Opening a photograph must not pull a 24 MP original across the practice
    network, so the viewer asks for this instead. Falls back to the original
    when there is no preview - an image small enough not to need one, or a RAW
    that could not be decoded - so a caller never has to special-case it.
    """
    with db_session() as conn:
        row = _load(conn, image_id)

    preview = row["preview_path"] if "preview_path" in row.keys() else None
    if preview:
        path = Path(preview)
        if path.exists():
            return FileResponse(path, media_type="image/jpeg")

    path = Path(row["stored_path"])
    if not path.exists():
        raise HTTPException(410, "Image file is no longer on disk")
    return FileResponse(path, media_type=row["mime"])


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
        # file that is actually there, and the ownership change still lands -
        # a chart pointing at the right patient with a slow-to-move file beats
        # one that silently never got assigned at all.
        target_patient = conn.execute(
            "SELECT * FROM patients WHERE id=?", (patient_id,)
        ).fetchone()
        result = move_image(
            Path(row["stored_path"]), folder_for(target_patient, row["captured_at"])
        )

        conn.execute(
            "UPDATE images SET session_id=?, patient_id=?, status='assigned',"
            " stored_path=?, filename=? WHERE id=?",
            (session_id, patient_id, str(result.path), result.path.name, image_id),
        )
        detail = f"from={previous or 'unassigned'} to={patient_id}"
        if not result.moved:
            detail += f" (file move failed: {result.error})"
        audit(conn, actor="ui", action="image.assign", subject=image_id, detail=detail)
        row = _load(conn, image_id)
        data = image_to_dict(row)

    if not result.moved:
        # The chart is correct; the file on disk is not filed under the
        # patient's folder yet. Say so, rather than a silent, misleading
        # success - a clinician acting on "this is filed" needs to know when
        # it is not actually true yet.
        data["warning"] = (
            "Assigned to the patient, but the file could not be moved into "
            "their folder yet (it may be locked by another program). It will "
            "stay findable by this record; try again shortly to re-file it."
        )

    broker.publish("image.assigned", data)
    return data
