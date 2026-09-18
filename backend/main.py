"""
main.py — FastAPI application for the dental camera workflow service.

Starts the file watcher or USB tethering loop depending on CAMERA_MODE,
runs the ingest pipeline for every new image, and exposes:

  GET  /           → serves the dashboard HTML
  GET  /status     → last N ingest results as JSON (polled by dashboard)
  GET  /config     → current active session config (read-only)
  POST /demo/ingest → (CAMERA_MODE=mock only) drop a demo image into inbox
  GET  /outbox/{path} → serves files from the outbox (for preview thumbnails)
"""

import asyncio
import json
import logging
import os
import random
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Minimal fallback to read .env without python-dotenv package
    env_file = Path(".env")
    if env_file.exists():
        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from carestack import make_carestack_client
from ingest import (
    Config,
    IngestResult,
    load_config,
    load_manifest,
    process_file,
    save_manifest,
)
from preview import generate_preview
from tether import start_tether, stop_tether
from watcher import start_watcher

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

CAMERA_MODE = os.getenv("CAMERA_MODE", "mock").lower()          # tether | watch | mock
INBOX_PATH = Path(os.getenv("INBOX_PATH", "./inbox"))
OUTBOX_PATH = Path(os.getenv("OUTBOX_PATH", "./outbox"))
MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", "./outbox/.manifest.json"))
DEMO_IMAGES_PATH = Path(os.getenv("DEMO_IMAGES_PATH", "./demo_images"))
SESSION_CONFIG_PATH = Path(os.getenv("SESSION_CONFIG_PATH", "./config.json"))

CARESTACK_MOCK = os.getenv("CARESTACK_MOCK", "true").lower() == "true"
CARESTACK_BASE_URL = os.getenv("CARESTACK_BASE_URL", "")
CARESTACK_CLIENT_ID = os.getenv("CARESTACK_CLIENT_ID", "")
CARESTACK_CLIENT_SECRET = os.getenv("CARESTACK_CLIENT_SECRET", "")

# Keep the last 200 results in memory for the dashboard
RESULT_HISTORY_SIZE = 200

# ---------------------------------------------------------------------------
# Shared state (populated at startup)
# ---------------------------------------------------------------------------

config: Config | None = None
manifest: dict = {}
carestack = None
result_history: deque[dict] = deque(maxlen=RESULT_HISTORY_SIZE)
_observer = None     # watchdog Observer (watch mode)
_tether_thread = None  # tether Thread (tether mode)


# ---------------------------------------------------------------------------
# Pipeline executor (called for every new image, from any source mode)
# ---------------------------------------------------------------------------

async def handle_new_image(source_path: Path) -> None:
    """
    Run the full ingest pipeline for one incoming image file.

    This coroutine is scheduled on the asyncio event loop from the background
    watcher/tether threads using loop.call_soon_threadsafe.
    """
    global manifest

    logger.info("Pipeline: processing %s", source_path.name)

    # Run the synchronous ingest step in a thread pool so we don't block the
    # event loop during file I/O (important for large RAW files)
    loop = asyncio.get_event_loop()
    result: IngestResult = await loop.run_in_executor(
        None, process_file, source_path, config, manifest, OUTBOX_PATH
    )

    if result.status == "ingested":
        # Persist updated manifest immediately
        await loop.run_in_executor(None, save_manifest, MANIFEST_PATH, manifest)

        # Generate preview (also blocking I/O, run in thread pool)
        preview_path = Path(result.preview_dest)
        try:
            await loop.run_in_executor(
                None,
                generate_preview,
                Path(result.original_dest),
                preview_path,
                config.preview_max_px,
                config.preview_quality,
            )
        except Exception as exc:
            logger.error("Preview failed for %s: %s", result.filename, exc)
            result.preview_dest = ""

        # Upload to CareStack
        try:
            upload_result = await carestack.upload_image(
                patient_id=result.patient_id,
                file_path=Path(result.original_dest),
                document_name=result.filename,
                visit_date=date.fromisoformat(result.capture_date) if result.capture_date else date.today(),
            )
            result.carestack_status = "mock" if upload_result.mock else (
                "uploaded" if upload_result.success else "error"
            )
        except Exception as exc:
            logger.error("CareStack upload exception: %s", exc)
            result.carestack_status = "error"

    # Store result for dashboard polling
    result_history.appendleft(_result_to_dict(result))
    logger.info("Pipeline done: %s → %s", result.filename, result.status)


def _schedule_from_thread(source_path: Path) -> None:
    """
    Thread-safe bridge: called by watcher/tether background threads to
    schedule handle_new_image on the asyncio event loop.
    """
    loop = asyncio.get_event_loop()
    asyncio.run_coroutine_threadsafe(handle_new_image(source_path), loop)


# ---------------------------------------------------------------------------
# Application lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, manifest, carestack, _observer, _tether_thread

    # Ensure directories exist
    INBOX_PATH.mkdir(parents=True, exist_ok=True)
    OUTBOX_PATH.mkdir(parents=True, exist_ok=True)

    # Load session config
    if SESSION_CONFIG_PATH.exists():
        config = load_config(SESSION_CONFIG_PATH)
        logger.info("Config loaded: %d sessions defined", len(config.sessions))
    else:
        logger.warning(
            "config.json not found at %s — copy config.json.example to config.json",
            SESSION_CONFIG_PATH,
        )
        # Use a safe fallback so the service still starts
        config = _default_config()

    # Load or initialise manifest
    manifest = load_manifest(MANIFEST_PATH)
    logger.info("Manifest loaded: %d known files", len(manifest))

    # Build CareStack client
    carestack = make_carestack_client(
        mock=CARESTACK_MOCK,
        base_url=CARESTACK_BASE_URL,
        client_id=CARESTACK_CLIENT_ID,
        client_secret=CARESTACK_CLIENT_SECRET,
    )

    # Start the appropriate input mode
    if CAMERA_MODE == "tether":
        logger.info("Starting in USB tether mode")
        _tether_thread = start_tether(INBOX_PATH, _schedule_from_thread)
    elif CAMERA_MODE == "watch":
        logger.info("Starting in folder-watch mode")
        _observer = start_watcher(INBOX_PATH, _schedule_from_thread)
    else:  # mock
        logger.info("Starting in mock / demo mode — use POST /demo/ingest to trigger")

    yield  # FastAPI serves requests here

    # Shutdown
    if _observer:
        _observer.stop()
        _observer.join()
    if _tether_thread:
        stop_tether(_tether_thread)

    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Dental Camera Workflow",
    description="Automates DSLR → CareStack patient image ingestion",
    version="1.0.0",
    lifespan=lifespan,
)

# Serve outbox files (preview thumbnails) under /outbox/
app.mount("/outbox", StaticFiles(directory=str(OUTBOX_PATH), html=False), name="outbox")

# Serve the frontend dashboard
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@app.get("/", include_in_schema=False)
async def dashboard():
    """Serve the dashboard HTML."""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(str(index))


@app.get("/status")
async def get_status():
    """
    Return the last N ingest results for the live dashboard.

    Polled every 2 seconds by the frontend.
    """
    active_sessions = [
        {"patient_id": s.patient_id, "patient_name": s.patient_name}
        for s in config.sessions
        if s.active
    ] if config else []

    return JSONResponse({
        "camera_mode": CAMERA_MODE,
        "carestack_mock": CARESTACK_MOCK,
        "inbox": str(INBOX_PATH.resolve()),
        "outbox": str(OUTBOX_PATH.resolve()),
        "total_ingested": sum(1 for r in result_history if r["status"] == "ingested"),
        "total_duplicates": sum(1 for r in result_history if r["status"] == "duplicate"),
        "total_errors": sum(1 for r in result_history if r["status"] == "error"),
        "active_sessions": active_sessions,
        "results": list(result_history),
    })


@app.get("/config")
async def get_config():
    """Return the current session configuration (read-only)."""
    if not config:
        raise HTTPException(status_code=503, detail="Config not loaded")
    return JSONResponse({
        "practice_id": config.practice_id,
        "sessions": [
            {
                "patient_id": s.patient_id,
                "patient_name": s.patient_name,
                "session_date": s.session_date,
                "filename_prefix": s.filename_prefix,
                "active": s.active,
            }
            for s in config.sessions
        ],
    })


@app.post("/demo/ingest")
async def demo_ingest(filename: str | None = None):
    """
    Demo mode: pick a file from demo_images/ and run it through the pipeline.

    If filename is provided, use that specific file.
    Otherwise, pick a random file from the demo_images/ directory.
    Available in all CAMERA_MODE values for demo flexibility.
    """
    if not DEMO_IMAGES_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail=f"demo_images/ directory not found at {DEMO_IMAGES_PATH.resolve()}. "
                   "Add some JPEG files there to use demo mode.",
        )

    demo_files = [
        f for f in DEMO_IMAGES_PATH.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ]

    if not demo_files:
        raise HTTPException(
            status_code=404,
            detail="No files found in demo_images/. Add JPEG files to use demo mode.",
        )

    if filename:
        target = DEMO_IMAGES_PATH / filename
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Demo file not found: {filename}")
    else:
        target = random.choice(demo_files)

    # Copy into inbox to simulate an SD card / camera event
    dest = INBOX_PATH / target.name
    import shutil
    shutil.copy2(target, dest)
    logger.info("Demo: copied %s → inbox/", target.name)

    # Schedule pipeline
    _schedule_from_thread(dest)

    return JSONResponse({"queued": target.name, "message": "File queued for ingestion"})


@app.post("/demo/ingest-all")
async def demo_ingest_all():
    """
    Demo mode: ingest all files from demo_images/ at once.
    Great for showing the 12-photo series workflow.
    """
    if not DEMO_IMAGES_PATH.exists():
        raise HTTPException(status_code=404, detail="demo_images/ not found")

    demo_files = [
        f for f in sorted(DEMO_IMAGES_PATH.iterdir())
        if f.is_file() and not f.name.startswith(".")
    ]

    if not demo_files:
        raise HTTPException(status_code=404, detail="No files in demo_images/")

    import shutil
    queued = []
    for src in demo_files:
        dest = INBOX_PATH / src.name
        shutil.copy2(src, dest)
        _schedule_from_thread(dest)
        queued.append(src.name)

    return JSONResponse({"queued": queued, "count": len(queued)})


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    Accept an uploaded image file (from a phone camera, browser picker, or device).
    Saves to INBOX_PATH and triggers the ingestion pipeline.
    """
    dest = INBOX_PATH / file.filename
    with open(dest, "wb") as buffer:
        import shutil
        shutil.copyfileobj(file.file, buffer)
    logger.info("Upload: saved %s to inbox/", file.filename)
    _schedule_from_thread(dest)
    return JSONResponse({"filename": file.filename, "message": "Uploaded and queued for ingestion"})


@app.post("/demo/reset")
async def reset_demo():
    """
    Reset the manifest and result history so files can be re-tested as new.
    """
    global manifest
    manifest = {}
    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()
    result_history.clear()
    logger.info("Demo reset: manifest and history cleared")
    return JSONResponse({"message": "Manifest and history reset successfully"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_to_dict(result: IngestResult) -> dict:
    return {
        "filename": result.filename,
        "status": result.status,
        "patient_id": result.patient_id,
        "patient_name": result.patient_name,
        "capture_date": result.capture_date,
        "original_dest": result.original_dest,
        "preview_dest": result.preview_dest,
        "carestack_status": result.carestack_status,
        "sha256": result.sha256[:8] + "…" if result.sha256 else "",
        "error_message": result.error_message,
        "processed_at": result.processed_at,
    }


def _default_config() -> Config:
    """Return a minimal config when config.json is missing."""
    from ingest import Config, SessionEntry
    return Config(
        practice_id="DEMO",
        sessions=[],
        default_patient_id="UNMATCHED",
        default_patient_name="Unmatched Patient",
        preview_max_px=2048,
        preview_quality=92,
        supported_extensions=[".jpg", ".jpeg", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".png"],
    )
