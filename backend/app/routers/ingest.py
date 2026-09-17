"""The ingest endpoint: one camera frame in, one charted image out.

Design notes worth defending in Q&A:

* The bridge never sends a patient id. It only knows *where* it is (operatory).
  The backend owns the mapping from operatory + time -> patient, so patient
  identity never has to travel to the capture device.
* Ingest is idempotent on the SHA-256 of the file bytes, which makes a retry
  after a half-failed upload a no-op instead of a duplicate chart entry.
* If nobody has opened a capture session, the image is quarantined rather than
  attached to a guess. A missing photo is recoverable; a photo in the wrong
  patient's chart is a clinical incident.

The work itself lives in `app.capture`, shared with manual upload so both
routes behave identically.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from starlette.concurrency import run_in_threadpool

from ..capture import chart_capture
from ..security import require_bridge_token

router = APIRouter(prefix="/api", tags=["ingest"])


@router.post("/ingest", dependencies=[Depends(require_bridge_token)])
async def ingest_image(
    file: UploadFile = File(...),
    source: str = Form("unknown"),
    operatory: str = Form("OP-1"),
    captured_at: str | None = Form(None),
    camera_make: str | None = Form(None),
    camera_model: str | None = Form(None),
    content_hash: str | None = Form(None),
):
    data = await file.read()

    # Decoding a 24 MP JPEG and writing SQLite are both blocking. Doing them on
    # the event loop would stall the SSE stream that the chair-side view depends
    # on, so the whole synchronous part runs on a worker thread.
    return await run_in_threadpool(
        chart_capture,
        data,
        file.filename,
        file.content_type,
        source=source,
        actor=f"bridge:{source}",
        operatory=operatory,
        captured_at=captured_at,
        camera_make=camera_make,
        camera_model=camera_model,
        expected_hash=content_hash,
    )
