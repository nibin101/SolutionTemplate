# Frontend — SnapChart chair-side view

The screen a clinician actually looks at: pick the patient in the chair, then
watch photographs appear as they are taken.

## Stack

- Framework / platform: none — plain HTML, CSS and ES modules-free JavaScript
- Styling: hand-written CSS with light/dark support
- HTTP client: `fetch`, plus `EventSource` for live updates

**Why no framework.** This runs on a clinic PC that may be old, locked down, and
without a toolchain. Three static files served by the backend means no build
step, no `node_modules`, nothing to install or keep patched, and a page that
loads instantly on hardware a practice already owns. The UI is small enough that
a framework would cost more than it saves.

## Local Setup

There is nothing to build. The backend serves this directory at `/`:

```powershell
..\scripts\start-backend.ps1
# then open http://127.0.0.1:8000
```

To serve it separately during development, any static server works — the backend
allows cross-origin requests from `localhost`:

```powershell
cd frontend\public
..\..\.venv\Scripts\python -m http.server 5173
```

Then set `API` at the top of `app.js` to `http://127.0.0.1:8000`.

## What is on screen

| Region | What it does |
|--------|--------------|
| **Bridge status pill** | Live camera-link health: ready, transferring, paused, offline, or failed transfers. Driven by the bridge's heartbeat over SSE, with a 15 s poll as a safety net. |
| **Patients** | Search, add a new patient, and per row: **Capture** starts a session, and clicking the name opens that patient's record. |
| **Live capture** | Photographs for the open session, newest first, appearing within about a second of the shutter. Click one to enlarge. |
| **Patient record** | Every photograph on file for one patient, the on-disk folder they are stored in, and **Start capture** — which opens a session, after which every photo taken on the attached camera or phone appears here on its own until Stop. A link to the phone page, scoped to this patient, sits beside it. |

### `capture.html` — the phone page

A second, mobile-first page served from the same origin. A phone on the practice
Wi-Fi opens it, picks the room, and taps one large shutter button; each photo is
pushed straight to the server and lands in the patient's folder. It shows who
the photo is going to *before* the shot is taken, and says plainly when nobody is
in the chair, so nothing is shot into a void the clinician thinks is a chart.
Failed sends keep the bytes in the browser and offer a Retry, which costs the
clinician nothing.

Start the backend with `scripts\start-backend.ps1 -Lan` to make it reachable.
| **Needs assignment** | Captures that arrived while no session was open. They are never guessed onto a chart — a human attaches them here. |

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `BACKEND_URL` | Only needed if the UI is served separately from the API | `http://localhost:8000` |

> Never commit real `.env` values — see the root `.gitignore`. A starting point
> is provided in `.env.example`.

## Tests & Lint

No test or lint tooling is configured for the frontend — it is three
dependency-free static files, and the behaviour that matters (session routing,
quarantine, dedupe) is tested in the backend suite where it is enforced. Adding
a JS toolchain here would mean adding Node to a stack that otherwise does not
need it.

## Project Layout

```
frontend/
├── public/
│   ├── index.html      chair-side layout
│   ├── styles.css      theme, grid, tiles, lightbox
│   ├── app.js          API calls, SSE handling, rendering
│   ├── capture.html    phone capture page
│   ├── capture.css     phone-only styles
│   └── capture.js      shutter, upload, retry
└── .env.example
```
