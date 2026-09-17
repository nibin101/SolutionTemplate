"""Patient directory.

In production this is the PMS's own patient index; here it is a small local
table so the workflow can be demonstrated end to end without a live PMS.
"""

from __future__ import annotations

import sqlite3
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from ..capture import chart_capture
from ..db import session as db_session, utcnow
from ..schemas import PatientIn
from ..serializers import image_to_dict, patient_to_dict
from ..storage import patient_folder

router = APIRouter(prefix="/api/patients", tags=["patients"])

# A clinician selecting a whole shoot at once is the normal case; the cap only
# stops a misclick on a 5,000-image folder from tying up the server.
MAX_UPLOAD_BATCH = 50


@router.get("")
def list_patients(q: str | None = None):
    sql = "SELECT * FROM patients"
    params: tuple = ()
    if q:
        sql += (" WHERE first_name LIKE ? OR last_name LIKE ?"
                " OR chart_number LIKE ?")
        like = f"%{q}%"
        params = (like, like, like)
    sql += " ORDER BY last_name, first_name"
    with db_session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [patient_to_dict(r) for r in rows]


@router.post("", status_code=201)
def create_patient(payload: PatientIn):
    patient_id = str(uuid.uuid4())
    with db_session() as conn:
        try:
            conn.execute(
                "INSERT INTO patients (id, chart_number, first_name, last_name,"
                " date_of_birth, created_at) VALUES (?,?,?,?,?,?)",
                (patient_id, payload.chart_number, payload.first_name,
                 payload.last_name, payload.date_of_birth, utcnow()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Chart number {payload.chart_number} already exists")
        row = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    return patient_to_dict(row)


@router.get("/{patient_id}")
def get_patient(patient_id: str):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Patient not found")
    return patient_to_dict(row)


@router.get("/{patient_id}/images")
def patient_images(patient_id: str):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM images WHERE patient_id=? ORDER BY captured_at DESC,"
            " received_at DESC",
            (patient_id,),
        ).fetchall()
    return [image_to_dict(r) for r in rows]


@router.get("/{patient_id}/folder")
def patient_photo_folder(patient_id: str):
    """Where this patient's photographs live on disk.

    Surfaced so a clinician can open the folder directly - the records have to
    remain usable if this application is ever unavailable.
    """
    with db_session() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Patient not found")

    from ..config import settings

    name = patient_folder(row["chart_number"], row["first_name"], row["last_name"])
    return {"folder": name, "path": str(settings.image_dir / name)}


@router.post("/{patient_id}/images", status_code=201)
async def upload_patient_images(patient_id: str, files: list[UploadFile] = File(...)):
    """Attach photographs to a patient by hand.

    This is the path for everything the automated pipeline cannot reach: a shoot
    from before the bridge was installed, a phone snap, an image emailed in by a
    referring practice. It runs through the same charting code as the bridge, so
    deduplication, EXIF handling and the audit trail are identical.
    """
    if not files:
        raise HTTPException(400, "No files were uploaded")
    if len(files) > MAX_UPLOAD_BATCH:
        raise HTTPException(400, f"Upload at most {MAX_UPLOAD_BATCH} images at a time")

    with db_session() as conn:
        if conn.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone() is None:
            raise HTTPException(404, "Patient not found")

    results = []
    for upload in files:
        data = await upload.read()
        outcome = await run_in_threadpool(
            chart_capture,
            data,
            upload.filename,
            upload.content_type,
            source="upload",
            actor="ui:upload",
            patient_id=patient_id,
        )
        results.append({"filename": upload.filename, **outcome})

    stored = sum(1 for r in results if r["status"] == "stored")
    return {
        "stored": stored,
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
        "rejected": [r for r in results if r["status"] == "rejected"],
        "images": [r["image"] for r in results if "image" in r],
    }
