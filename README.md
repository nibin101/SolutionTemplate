# SnapChart

### **DSOLVE 2026** · DRISHTI · College of Engineering Trivandrum (CET)

**BUILD. SOLVE. DEMONSTRATE.**

|                   |                                                     |
| ----------------- | --------------------------------------------------- | 
| **Problem:**      | Problem 4 — Automated Camera Workflow               | 
| **Team Name:**    | CJD                                                 |
| **Team Members:** | Ajo Jose · Woitiwe Simson · Alan Joy · Nibin Thomas|
| **Institution:**  | CET                                                 |
| **Live Demo:**    | [Demo link goes here]                               |
| **Pitch Video:**  | [Social media pitch video link]                     |

---

## Table of Contents

- [Problem Statement](#problem-statement)
- [Our Solution](#our-solution)
- [Key Features](#key-features)
- [Screenshots & Demo](#screenshots--demo)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
- [Usage / Demo Script](#usage--demo-script)
- [Limitations & Future Scope](#limitations--future-scope)
- [Team](#team)
- [Submission Checklist](#submission-checklist)

---

## Problem Statement

> ## Problem 4: Automated Camera Workflow
>
> Develop an automated camera workflow that enables clinical teams to capture and
> transfer patient images directly from professional cameras into a dental software
> application.
>
> Currently, clinical teams may need to manually capture, transfer, organise, and
> upload patient images, and workflows can vary across camera brands.
>
> The solution should support commonly used professional DSLR cameras such as Canon,
> Nikon, and Sony, and enable seamless streaming or transfer of patient images
> directly into the application, reducing manual steps and improving the clinical
> photography workflow.

### Why this matters

Clinical photography is routine dentistry — records, diagnosis, treatment
planning, before-and-after. But the photographs live on a camera and the chart
lives in the practice software, and nothing joins them. Today someone ejects the
SD card, copies files to a PC, sorts them into folders, works out which shots
belong to which patient, and uploads them. That is several minutes of unbilled
admin per patient, done at the end of a long day, and every step is a chance to
put a photograph in the wrong chart.

---

## Our Solution

**SnapChart is a background service that turns "take the photo" into the whole
workflow.** Nothing to open, nothing to copy, nothing to sort.

It is two pieces:

- **The camera bridge** — a background app on the operatory PC. It watches for
  new photographs from whatever camera is attached, over whatever connection the
  practice uses, and pushes each one to the chart within about a second of the
  shutter. It starts at login and sits in the tray.
- **The backend + chair-side view** — receives images, works out which patient
  they belong to, stores them, and shows them appearing live on screen.

Three decisions define it:

**One implementation covers all three brands.** Rather than integrating Canon
EDSDK, the Nikon SDK and Sony's Camera Remote SDK — three registration-walled
native libraries — we target **PTP over USB through Windows Portable Devices**,
the vendor-neutral API Windows already exposes for every one of those bodies. No
licence, no vendor driver, no per-brand code path. Vendor SDKs remain available
as optional adapters for what only they can do (the Canon EDSDK adapter is
implemented, for push-on-shutter capture), but nothing depends on them.

**The camera never knows who the patient is.** The bridge is told *when*, never
*who*: the backend sends down the start and end of the capture session running
in that room, and the bridge transfers the photographs taken between them.
Anything outside that window is **quarantined for a human, not guessed onto a
chart** — because a missing photo is an inconvenience and a photo in the wrong
patient's record is a clinical incident.

**Nothing is in flight that is not also on disk.** Every capture is spooled
locally before an upload is attempted and removed only when the backend confirms
it. Power cuts, dropped Wi-Fi and a backend restart cost nothing; duplicates are
impossible because delivery is idempotent on the image's SHA-256.

Design decisions in full, including the trade-offs we rejected:
**[docs/architecture.md](./docs/architecture.md)**.

---

## Key Features

- **Brand-agnostic USB capture** — Canon, Nikon and Sony over one MTP/PTP
  implementation, with a Windows Shell fallback for awkward bodies.
- **Every connection method, at once** — USB tethering, vendor tether-software
  folders, camera Wi-Fi/FTP drops, and SD cards all feed the same pipeline and
  can run simultaneously.
- **True background operation** — starts at login with no installer and no admin
  rights, runs from the system tray with link status, pause and retry.
- **Automatic patient association** — press *Start capture*, take the photos,
  press *Stop*. Everything shot in between charts itself to the patient in that
  chair; anything outside the window is quarantined rather than guessed.
- **Live chair-side view** — photographs appear on screen as they are taken, over
  Server-Sent Events.
- **Records you can still read without us** — photographs are filed on disk as
  `photos/<name>_<chart>/<date>/`, one folder per person named after them, and
  are moved into place when a quarantined image is assigned.
- **No files to pick, and no button to press** — nothing is browsed, chosen or
  fetched. The bridge watches whatever camera or phone is plugged into the PC
  and sends across the shots taken during the session, on its own.
- **Never imports what it was not asked for** — plugging a phone in imports
  *none* of its camera roll, because none of it was taken while a patient was in
  the chair. That is not a setting to get wrong; it falls out of the rule.
- **Add patients from the chair** — register a new patient without leaving the
  capture view.
- **Phone as a wireless camera** — open one page on any phone on the practice
  Wi-Fi and photographs go straight into the patient's folder on the practice
  PC. No app to install, no cable, no file to move; it routes by operatory
  exactly as the USB bridge does.
- **Failure-proof delivery** — durable on-disk spool, exponential backoff, dual
  content-hash deduplication, transit-corruption detection, and a visible failed
  queue that never discards an image.
- **Visible health** — a 10-second heartbeat puts camera-link status on the
  clinician's screen, so a dead link is noticed immediately, not at the end of a
  shoot.
- **Demo without hardware** — a simulator source drives the identical pipeline.

---

## Screenshots & Demo

| Screenshot                                            | Description                          |
| ----------------------------------------------------- | ------------------------------------ |
| [Screenshot 1](./assets/screenshots/screenshot-1.png) | Chair-side view with a live capture session |
| [Screenshot 2](./assets/screenshots/screenshot-2.png) | Tray icon and camera-link status     |
| [Pitch Video](./assets/pitch/README.md)               | Link to your >30s social pitch video |

---

## Tech Stack

| Layer           | Technology                         | Why we chose it |
| --------------- | ---------------------------------- | --------------- |
| Frontend        | Plain HTML / CSS / JavaScript      | Runs on a locked-down clinic PC with no build step, no `node_modules`, nothing to patch |
| Backend         | Python 3.12 · FastAPI · Uvicorn    | Multipart ingest, SSE and OpenAPI docs out of the box; same language as the bridge |
| Database        | SQLite (WAL) + content-addressed files | Single-site, low-concurrency reality of one practice; no server to operate |
| Camera access   | Windows Portable Devices (MTP/PTP) via `comtypes`; `watchdog`; `ctypes` → Canon EDSDK | One vendor-neutral API covers Canon, Nikon and Sony; SDKs stay optional |
| Background app  | `pystray` tray icon + per-user autostart | Invisible day to day, but accountable when something breaks |
| Infra / Hosting | Runs on the practice's own PC      | Patient images never leave the clinic |

---

## Getting Started

### Prerequisites

- **Python 3.12+** on PATH
- **Windows 10/11** for USB camera capture (`folder` and `simulator` sources run
  on any OS)
- A Canon / Nikon / Sony camera and a USB cable — optional; the simulator
  replaces it for a full demo

### Installation

```powershell
git clone <this-repo>
cd SolutionTemplate
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` creates a virtualenv, installs both components, generates a shared
`BRIDGE_TOKEN` into `backend\.env` and `bridge\.env`, and seeds demo patients.
It is safe to re-run; existing `.env` files are left alone.

Then start it — one command:

```powershell
scripts\start.ps1                       # opens http://127.0.0.1:8000
scripts\start.ps1 -Simulate             # demo mode, no camera needed
scripts\stop.ps1                        # stop both
```

That launches the backend, waits until it actually answers, starts the bridge
and opens the chair-side view. Each service keeps its own window so you can
still read its log.

<details>
<summary>Starting the two services separately</summary>

They are separate processes on purpose: in a practice, one backend serves the
whole clinic and a bridge runs on each operatory PC. To run them individually —
or on different machines — use:

```powershell
scripts\start-backend.ps1               # http://127.0.0.1:8000
scripts\start-bridge.ps1 -Simulate      # or omit -Simulate for a real camera
scripts\start-bridge.ps1 -Tray          # background, system-tray icon
```

</details>

To check what a particular machine can do before trusting it:

```powershell
scripts\start-bridge.ps1 -Check
```

### Using a phone as the camera

```powershell
scripts\start.ps1 -Lan
```

This binds the server to the local network (for that run only) and prints the
address to open. On any phone on the same Wi-Fi, open
`http://<that-address>:8000/capture.html`, pick the room, and shoot — each photo
is pushed straight into the patient's folder on this PC. Windows Firewall needs
inbound TCP 8000 allowed once, in an **admin** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "SnapChart 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

To run it the way a practice would — in the background, from login:

```powershell
cd bridge
..\.venv\Scripts\python run_bridge.py --install-autostart
..\.venv\Scripts\python run_bridge.py --tray
```

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `BRIDGE_TOKEN` | Shared secret between bridge and backend. Must match in both `.env` files. | `a-long-random-string` |
| `OPERATORY` | Which room the capture PC is in — how a photo finds its patient | `OP-1` |
| `ENABLED_SOURCES` | Capture transports to run | `wpd,folder,removable` |
| `BACKEND_URL` | Where the bridge sends captures | `http://127.0.0.1:8000` |
| `DATA_DIR` | Backend database and image store | `./data` |
| `QUARANTINE_UNASSIGNED` | Hold un-sessioned captures for review instead of guessing | `true` |

Full tables: **[backend/README.md](./backend/README.md)** ·
**[bridge/README.md](./bridge/README.md)**.

> Never commit real keys: use a `.env` file (already gitignored) or `.env.example`.

### Tests

```powershell
.venv\Scripts\python -m pytest backend/tests bridge/tests -q
```

---

## Usage / Demo Script

_This doubles as the live demo runbook (3–5 min)._

1. **Boot** — `scripts\start.ps1` (add `-Simulate` if there is no camera). The
   backend, the camera bridge and the chair-side view all come up from one
   command. The status pill goes to *Camera ready* with the camera's own model
   name. Point out that from here on nobody touches the app.
3. **Take a photo with no session open** — nothing happens, and that is the
   point. The status line reads *no capture session open*; the shot stays on the
   camera. Plug in a phone at this point and show that none of its camera roll
   is imported either. *This is the safety property: the system takes nothing
   while nobody is in the chair, and never guesses a patient.*
4. **Press Start capture on the patient in the chair** — take the next photo; it
   lands in the live grid within about a second, tagged with the camera model
   and EXIF capture time, with nobody having touched a file. Press **Stop**, take
   one more, and show that it does *not* go on the chart. One rule, visible in
   both directions: the photographs taken between Start and Stop, and no others.
5. **The "wow" moment** — pull the USB cable (or stop the backend) mid-series
   and keep shooting. Nothing is lost: the tray goes amber, the queue holds the
   images on disk, and on reconnection they drain into the chart in order. Then
   re-send a photo that already arrived — it is recognised by content hash and
   charted once.
6. **Wrap-up** — assign that last photo to its patient by hand, showing the
   audit trail, and open `backend\data\photos\` to show the records filed
   under the person's own name — readable without this software at all. In production the same `/api/ingest` call writes to the practice
   management system; the bridge does not change.

---

## Limitations & Future Scope

### Known Limitations

- The `wpd` USB path is verified end to end against a real MTP/PTP device over
  USB, but not yet against an actual DSLR body — a camera differs mainly in tree
  layout, and the walk makes no assumption about it. The `shell` fallback and
  the Canon EDSDK adapter are exercised only through their no-device paths.
  The spool, uploader, folder watcher, simulator and the whole backend are
  covered end to end.
- Nikon and Sony vendor adapters are deliberately unimplemented; those brands
  transfer through the generic `wpd` path. Writing SDK bindings without the NDA
  headers would produce code that looks finished and cannot work.
- USB and SD-card capture are Windows-only. `folder` and `simulator` run anywhere.
- The bridge→backend hop uses a shared secret; there is no clinician login yet.
- Single-site assumptions: SQLite, local image storage, one clinic.

### Future Scope

- Forward charted images into a real PMS document API behind `/api/ingest`.
- Clinician authentication and role-based access on the chair-side view.
- Automatic labelling of the standard five-view series (frontal, occlusal,
  buccal) from capture order and image content.
- Encryption at rest for the spool and the image store.

---

## Team

| Name     | Role(s)                         | GitHub    | Email   |
| -------- | ------------------------------- | --------- | ------- |
| [Name 1] | [e.g. Full-stack / ML / Design] | [@handle] | [email] |
| [Name 2] |                                 |           |         |

---

## Submission Checklist

**Before 6:00 AM (Code Freeze) – Sat, Sept 19th:**

- [ ] Clean, runnable source code committed to this **public** repo
- [ ] `README.md` fully filled in (all sections above)
- [ ] Pitch video (>30s, English) posted on team member's social profile
      tagging **@DrishtiCET** & **@CareStack** and link added above
- [ ] All secrets/API keys removed from the repo
- [ ] Quick-start verified from a fresh clone (`git clone` → run)

---

**[Architecture & Decisions](./docs/architecture.md)** ·
**[Problem Statements](./docs/problem-statements.md)** ·
**[Submission Checklist](./SUBMISSION_CHECKLIST.md)** ·
**DSOLVE 2026 Guidelines**
