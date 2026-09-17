# Backend — SnapChart

The ingest API and system of record. It accepts images from the camera bridge,
decides which patient each one belongs to, stores it, and pushes it live to the
chair-side UI (which it also serves).

## Stack

- Language / runtime: Python 3.12
- Framework: FastAPI + Uvicorn
- Database: SQLite (standard-library `sqlite3`, WAL mode)
- Images: content-addressed files on disk, thumbnails and EXIF via Pillow
- Live updates: Server-Sent Events

SQLite and the local filesystem are deliberate. A dental operatory is a
single-site, single-digit-concurrency environment; adding a database server
would add an operational failure mode without adding capability. The data access
layer is thirty lines of plain SQL, so moving to Postgres later is a connection
string and a migration, not a rewrite.

## Local Setup

```powershell
cd backend
..\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then set a real BRIDGE_TOKEN
..\.venv\Scripts\python seed.py        # a few demo patients
..\.venv\Scripts\python run_server.py
```

Then open <http://127.0.0.1:8000> — the API serves the chair-side UI from
`frontend/public`. Interactive API docs are at `/docs`.

## Where the photographs live

Images are filed where a human can find them without the application:

```
backend/data/
├── snapchart.db
├── photos/
│   ├── Aarav_Menon_CS-1001/2026-09-18/IMG_0001.JPG
│   ├── Meera_Krishnan_CS-2010/2026-09-18/frontal.jpg
│   └── _unassigned/2026-09-18/IMG_0007.JPG
└── thumbnails/a3/<sha256>.jpg
```

One folder per person, named after them, with their chart number on the end
because two patients really can share a name; one sub-folder per day of capture. A practice that has to hand records to a
specialist — or that loses this software entirely — still has an organised
folder of clinical photographs. That is worth more than the tidiness of a
content-addressed blob store, so deduplication is handled by the unique index on
the image's SHA-256 in the database instead of by the file path.

Quarantined captures live under `_unassigned/` and are **moved** into the
patient's folder when someone assigns them, so the folder tree always matches
the chart. Thumbnails stay content-addressed and separate, so the photo folders
contain only real photographs.

## How a photo finds its patient

This is the part that matters clinically, so it is worth stating plainly.

1. The bridge **never sends a patient id.** It only reports which room it is in
   (`operatory`). Patient identity never travels to the capture device.
2. The backend keeps at most one **open capture session** per operatory —
   opening a new one automatically closes the previous one, so a clinician who
   forgets to press Stop cannot cross-contaminate two charts.
3. An arriving image is charted to the session whose **time window contains the
   moment it was taken** — `started_at` to `ended_at`, with 90 seconds of slack
   either side for camera clock drift. Matching a timestamp to a window is not a
   guess, so this holds for a session that has just closed too: a shot taken a
   second before Stop, and read off the camera a second after, still lands on
   the right chart.
4. If the photograph falls in **no** session's window, it is **quarantined**,
   not guessed. It appears in "Needs assignment" in the UI and is attached by a
   human.

Point 4 is the design's central trade-off: a missing photo is a minor
inconvenience, a photo in the wrong patient's chart is a clinical incident. The
system is built to fail towards the first. (`QUARANTINE_UNASSIGNED=false`
additionally accepts a late arrival from *outside* the window, within 10 minutes
of a session closing, for practices that prefer it.)

The bridge applies the same rule before transferring anything, using the window
it gets back on each heartbeat — so the check runs twice, and the server, which
is the side that has to be right, never takes the bridge's word for it.

Every ingest, assignment and reassignment is written to an append-only
`audit_log` table holding ids only — never names.

## API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/ingest` | Bridge → backend. Multipart image + capture metadata. Requires `X-Bridge-Token`. Idempotent on the image's SHA-256. |
| `GET`/`POST` | `/api/patients` | Patient directory (stands in for the PMS index). `GET` accepts `?q=` to search name or chart number. |
| `GET` | `/api/patients/{id}/images` | Everything charted to one patient. |
| `POST` | `/api/patients/{id}/images` | Upload photographs to a patient by hand (multipart, up to 50 at once). Same charting code as the bridge. |
| `GET` | `/api/patients/{id}/folder` | Where that patient's photographs live on disk. |
| `POST` | `/api/sessions` | Open a capture session (patient + operatory). |
| `POST` | `/api/sessions/{id}/end` | Close it. |
| `GET` | `/api/sessions/active?operatory=` | Who is in the chair right now. |
| `GET` | `/api/images/unassigned` | The quarantine queue. |
| `POST` | `/api/images/{id}/assign` | Attach a quarantined image to a patient or session. |
| `GET` | `/api/images/{id}/file` · `/thumb` | Full image and preview. |
| `POST` | `/api/capture` | Phone-as-camera. Multipart push from a handset on the practice Wi-Fi; routes by `operatory` like the bridge, or by `patient_id` if given. |
| `POST` | `/api/bridge/heartbeat` | Bridge liveness + source status. Requires `X-Bridge-Token`. The **reply carries the capture window** — `since`/`until` for the session running in that operatory, and **never the patient**. |
| `GET` | `/api/bridge/status` | Is the camera link up? |
| `GET` | `/api/events` | SSE stream of captures, assignments and bridge status. |

### Integrating with a real PMS

`/api/ingest` is the seam. In a production deployment the same call either
writes to the PMS's document API instead of local storage, or this service runs
alongside it as a capture buffer and syncs on a schedule. Nothing in the bridge
changes — it knows only this one endpoint.

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `BRIDGE_TOKEN` | Shared secret the bridge must present | `a-long-random-string` |
| `DATA_DIR` | Where SQLite and images live | `./data` |
| `HOST` / `PORT` | Listen address | `127.0.0.1` / `8000` |
| `SERVE_FRONTEND` | Serve `frontend/public` at `/` | `true` |
| `QUARANTINE_UNASSIGNED` | Hold un-sessioned captures for review | `true` |

> Never commit real `.env` values — see the root `.gitignore`. A starting point
> is provided in `.env.example`.

## Tests

```powershell
..\.venv\Scripts\python -m pytest tests -q
```

The suite covers the safety rules above: authentication, quarantine, per-room
session routing, idempotent re-delivery, corrupt-transfer rejection, manual
assignment, EXIF extraction and bridge liveness.

## Project Layout

```
backend/
├── run_server.py        entry point
├── seed.py              demo patients
├── app/
│   ├── main.py          app wiring, SSE stream, static UI mount
│   ├── config.py        settings from .env
│   ├── db.py            schema + connection handling + audit trail
│   ├── capture.py       charting a photo - shared by the bridge and by upload
│   ├── storage.py       per-patient folder layout, thumbnails, EXIF
│   ├── security.py      bridge token check
│   ├── events.py        SSE broker
│   ├── schemas.py       request models
│   ├── serializers.py   row → JSON
│   └── routers/         ingest · patients · sessions · images · bridge
├── tests/
└── .env.example
```
