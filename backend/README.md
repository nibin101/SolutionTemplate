# Backend — Dental Camera Workflow

Automated dental clinical photography pipeline for CareStack.

## Stack

- **Language / runtime:** Python 3.11
- **Framework:** FastAPI + uvicorn
- **File watching:** watchdog (inotify / FSEvents / ReadDirectoryChangesW)
- **EXIF reading:** exifread
- **Image ops:** Pillow (preview generation — originals never modified)
- **RAW decode:** rawpy (libraw)
- **HTTP client:** httpx (async, for CareStack API)
- **Computer vision:** opencv-python-headless (Haar face detection + Laplacian blur; pinned <5.0 as Haar was removed in OpenCV 5)
- **Tests:** pytest + pytest-asyncio

## System Prerequisites

```bash
# Ubuntu / Debian (for RAW file support)
sudo apt install libraw-dev gphoto2 libgphoto2-dev

# macOS (Homebrew)
brew install libraw gphoto2
```

> RAW support (CR2, CR3, NEF, ARW) requires `libraw-dev`. JPEG-only mode
> works without it.

> USB tethering requires `gphoto2`. If GNOME auto-mounts your camera, run:
> `sudo pkill gvfsd-gphoto2` before starting.

## Local Setup

```bash
cd backend

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment file
cp .env.example .env

# Copy and edit session config
cp config.json.example config.json
# Edit config.json: set the active patient session and filename prefix

# Create inbox / outbox directories
mkdir -p inbox outbox

# Start the server
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` for the live dashboard.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `CAMERA_MODE` | `tether` / `watch` / `mock` | `mock` |
| `INBOX_PATH` | Directory to watch for new images | `./inbox` |
| `OUTBOX_PATH` | Root of organized output | `./outbox` |
| `MANIFEST_PATH` | Duplicate-detection manifest file | `./outbox/.manifest.json` |
| `SESSION_CONFIG_PATH` | Patient session config JSON | `./config.json` |
| `CARESTACK_MOCK` | `true` → no real HTTP calls | `true` |
| `CARESTACK_BASE_URL` | CareStack API base URL | — |
| `CARESTACK_CLIENT_ID` | OAuth2 client ID | — |
| `CARESTACK_CLIENT_SECRET` | OAuth2 client secret | — |
| `PORT` | Server port | `8000` |

## Running Tests

```bash
cd backend
pip install piexif  # needed for EXIF test fixtures
pytest -v
```

All tests run without a camera, without CareStack credentials, and without
any files outside of pytest's temporary directories.

### Key test — zero quality loss proof

```
tests/test_ingest.py::TestProcessFile::test_original_file_unchanged_after_copy
tests/test_preview.py::TestGeneratePreview::test_original_file_unchanged_after_preview
```

These tests compare SHA-256 hashes before and after the pipeline to prove
the original file is never modified.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Live dashboard |
| `GET` | `/status` | Last 200 ingest results (JSON) |
| `GET` | `/config` | Current session config |
| `POST` | `/demo/ingest` | Trigger one demo image through pipeline |
| `POST` | `/demo/ingest-all` | Trigger all demo images |
| `GET` | `/docs` | Auto-generated API docs (Swagger UI) |

## Project Layout

```
backend/
├── main.py              # FastAPI app — wires everything together
├── ingest.py            # Core pipeline: hash, EXIF, patient resolve, copy, quality gate
├── preview.py           # Safe preview generation (never modifies original)
├── quality.py           # Blur + face detection quality gate (OpenCV Haar + Laplacian)
├── carestack.py         # CareStack API client + MockCareStackClient
├── watcher.py           # watchdog filesystem event handler
├── tether.py            # gphoto2 USB tethering
├── config.json.example  # Session config template (includes "quality" block)
├── pytest.ini
├── requirements.txt
├── .env.example
└── tests/
    ├── conftest.py      # Shared fixtures + quality test fixtures
    ├── test_ingest.py
    ├── test_preview.py
    ├── test_carestack.py
    └── test_quality.py   # New: unit + integration tests for the quality gate
```