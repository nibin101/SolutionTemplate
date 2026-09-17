"""Phone-as-camera: photographs pushed straight from a handset into the chart.

Why this exists alongside the USB bridge
----------------------------------------
A DSLR gives you image quality; a phone gives you reach. Not every operatory has
a camera on a cable, and plenty of useful clinical photographs are opportunistic
- a healing check, a fractured cusp spotted mid-appointment. This endpoint lets
any phone on the practice Wi-Fi send a photo directly into the patient's folder
on the practice PC, with no cable, no app to install and no folder to sort.

It is a *push*: the phone uploads to the server. Nothing is scraped off the
handset, and no file has to be dropped somewhere for a watcher to find.

Routing is identical to the camera bridge - the phone reports which operatory it
is in, and the backend maps that to the open capture session. Send a patient id
instead and it charts straight to that patient. Either way it is the same
`chart_capture` code, so deduplication, EXIF handling, folder layout and the
audit trail do not vary by which device took the photograph.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from ..capture import chart_capture
from ..db import session as db_session

router = APIRouter(prefix="/api/capture", tags=["mobile"])

# A phone sends one or two shots at a time; a gallery pick may send more.
MAX_BATCH = 30


@router.post("", status_code=201)
async def capture_from_phone(
    files: list[UploadFile] = File(...),
    operatory: str = Form("OP-1"),
    patient_id: str | None = Form(None),
    device: str | None = Form(None),
):
    if not files:
        raise HTTPException(400, "No files were uploaded")
    if len(files) > MAX_BATCH:
        raise HTTPException(400, f"Send at most {MAX_BATCH} photos at a time")

    if patient_id:
        with db_session() as conn:
            known = conn.execute(
                "SELECT 1 FROM patients WHERE id=?", (patient_id,)
            ).fetchone()
        if known is None:
            raise HTTPException(404, "Patient not found")

    results = []
    for upload in files:
        data = await upload.read()
        outcome = await run_in_threadpool(
            chart_capture,
            data,
            upload.filename,
            upload.content_type,
            source="mobile",
            actor=f"mobile:{device or 'phone'}",
            operatory=operatory,
            patient_id=patient_id,
            camera_model=device,
        )
        results.append({"filename": upload.filename, **outcome})

    return {
        "stored": sum(1 for r in results if r["status"] == "stored"),
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
        "rejected": [r for r in results if r["status"] == "rejected"],
        "images": [r["image"] for r in results if "image" in r],
    }
