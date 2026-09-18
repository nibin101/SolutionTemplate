# SnapChart — architecture and design decisions

Problem 4, *Automated Camera Workflow*: get clinical photographs off a
professional DSLR and into the right patient's chart, automatically, across
Canon, Nikon and Sony.

---

## The shape of the system

```
   Canon / Nikon / Sony            capture PC (operatory)                 clinic
   ┌──────────────────┐     ┌───────────────────────────────┐   ┌────────────────────┐
   │  USB  MTP/PTP    │────▶│  CAMERA BRIDGE (background)   │   │  BACKEND           │
   │  SD card / DCIM  │────▶│                               │   │                    │
   │  Wi-Fi / tether  │────▶│  sources ─▶ spool ─▶ uploader │──▶│  /api/ingest       │
   └──────────────────┘     │            (disk)    (retry)  │   │  session routing   │
                            │            tray icon          │◀──│  SQLite + files    │
                            └───────────────────────────────┘   │  SSE ─▶ chair-side │
                                       heartbeat                └────────────────────┘
```

Two processes, one contract between them: `POST /api/ingest` with an image and
where it was taken. Everything else is an implementation detail on one side or
the other.

---

## The nine problems, and what we did about each

### 1. Fragmented vendor ecosystems

**Decision: target PTP over USB through Windows Portable Devices, not three
vendor SDKs.**

Canon EDSDK, the Nikon SDK and Sony's Camera Remote SDK are three
registration-walled native libraries with three different threading models, and
all three would have to be licensed, shipped and kept in step with firmware. But
every one of those bodies also speaks PTP over USB, and Windows exposes PTP
through a single vendor-neutral COM API. One implementation, three brands, no
licence, no extra driver on a clinic PC.

Vendor SDKs are not dismissed — they are demoted to *adapters*
(`bridge/bridge/sources/vendor/`). The Canon EDSDK adapter is implemented,
because EDSDK buys something PTP cannot give: a push event the instant the
shutter closes, and transfer straight out of the body without the card. Nikon
and Sony adapters are deliberately **not** faked; their modules say what would
be required and why they are unimplemented.

Everything sits behind one interface — `CaptureSource.run(stop_event, emit)`.
Adding a brand is one file; nothing downstream changes.

### 2. Manual, multi-step transfer

**Decision: a background service, not an application anyone opens.**

The bridge starts at login (a per-user `Run` key — no installer, no admin
rights), lives in the tray, and needs no interaction. Card → computer → folder →
upload becomes: take the photo. The tray icon exists only so the workflow is
*accountable*: colour-coded link health, pause, retry, open chart view.

### 3. No automatic patient association

**Decision: the bridge is told when, never who. One rule decides everything.**

> A photograph is transferred and charted if it was taken between *Start
> capture* and *Stop*. Nothing else is touched.

The bridge reports `operatory`, not a patient. The backend keeps at most one
**open capture session** per operatory — opening one closes the previous session
in that room, so a clinician who forgets to press Stop cannot cross two charts —
and sends that session's start and end back down on each heartbeat reply. Two
timestamps, no identity. The bridge then filters the device by the camera's own
clock, and the server applies the same test again at ingest, because the server
is the side that has to be right.

**If a photograph falls in no session's window, it is quarantined, not guessed.**
It waits in "Needs assignment" for a human. This is the system's central
trade-off, and it is deliberate: a missing photo is an inconvenience; a photo in
the wrong patient's chart is a clinical incident. Every assignment is written to
an append-only audit log holding ids only, never names.

Three consequences worth stating, because they are what the rule buys:

- **Plugging a phone in imports nothing.** Every photo already on it was taken
  while nobody was in the chair. Hardware testing is what forced this: a phone
  plugged in for a five-minute test had its owner's camera roll pulled into the
  system, which is exactly the failure a dental practice cannot have. It is no
  longer a setting that can be got wrong.
- **An undated photograph is never charted.** If a device reports no usable
  timestamp, we cannot show the photo belongs to this patient — and an
  unprovable photograph in a dental record is worse than a missing one.
- **A shot taken after the window closed is held, not discarded.** It may be the
  *next* patient's first photograph, so it stays up for judgement instead of
  being written off. Ninety seconds of slack either side absorbs camera clock
  drift without ever reaching into the neighbouring appointment.

**What this replaced.** An earlier version had a *Capture images* button, a
command queue in the database, and the bridge polling for work to do. It worked,
but it asked the clinician to do something the system could work out for itself,
and "fetch the newest five" is wrong whenever the patient had four taken. The
window was always the real rule; once it was stated plainly, the queue, its
table, its four endpoints and the button all turned out to be scaffolding around
it. The bridge still never listens on a port — it polls outward, on the
heartbeat it was already sending.

### 4. Real-time vs. batch

**Decision: event-driven, with batch as the degraded case — not a separate mode.**

Sources emit the moment an image exists, the spool drains continuously, and the
chair-side UI updates over SSE within about a second of the shutter. When the
network is down the same queue simply accumulates and drains later — which *is*
batch, with no second code path to write or test. The Canon adapter takes this
further: EDSDK pushes on shutter-close rather than being polled.

### 5. Connection method trade-offs

**Decision: support all of them, concurrently, through the same pipeline.**

`ENABLED_SOURCES` turns on any combination of USB MTP (`wpd`, `shell`), watched
folders for tether software and Wi-Fi/FTP drops (`folder`), SD cards and
mass-storage bodies (`removable`), and a simulator. They run at once and share
one spool, so a practice can tether in one room, use a card reader in another,
and change its mind without touching code. Dedupe by content hash makes
overlapping sources harmless.

There is a fifth route that does not go through the bridge at all: a phone on
the practice Wi-Fi opens `capture.html` and **pushes** photographs to
`POST /api/capture`. Nothing is installed on the handset and nothing is scraped
off it. That matters because a DSLR gives you image quality but a phone gives
you reach — not every operatory has a camera on a cable, and plenty of useful
clinical photographs are opportunistic. It routes by operatory exactly like the
bridge, so a phone shot and a tethered shot are indistinguishable once charted.

### 6. Integration point with the PMS

**Decision: make `/api/ingest` the seam, and build the chart view behind it.**

Without published CareStack ingestion APIs, the honest move is to define the
contract we control and keep everything vendor-specific on one side of it. The
backend here is a working stand-in for the PMS: patients, sessions, charted
images, audit trail. In a real deployment `/api/ingest` either forwards to the
PMS document API or this service runs alongside as a capture buffer and syncs.
**The bridge does not change either way** — it knows exactly one endpoint.

No screen-scraping and no simulated UI interaction: those break on the next
release of somebody else's software.

### 7. Session / context management

Covered by the session model in (3). Sessions are per-operatory, so a busy clinic
day with several rooms works without the rooms interfering; the UI's operatory
selector switches which room's session you are looking at. Capture can be paused
from the tray when a clinician wants photographs that should *not* be charted.

### 8. Reliability and failure handling

**Decision: nothing is in flight that is not also on disk.**

- Every capture is written to the spool **before** an upload is attempted, and
  removed only after the backend confirms it. Power loss costs nothing.
- Retries use exponential backoff with jitter, capped at five minutes.
- After `MAX_UPLOAD_ATTEMPTS` an item is parked as *failed* **with its bytes
  intact**, and surfaced in the tray and the UI. Nothing is silently dropped.
- Duplicates are caught twice: by content hash in the bridge (never queue the
  same bytes) and again in the backend (never chart them). A retry of an upload
  that actually landed is a no-op, which is what makes retrying safe at all.
- The hash travels with the upload; a mismatch is refused rather than charted.
- Sources are supervised: one that crashes or whose camera is unplugged is
  restarted with backoff, without touching the others.
- A heartbeat every 10 s means a dead link shows up on the clinician's screen
  rather than being discovered at the end of a shoot.

### 9. Cross-platform / hardware constraints

**Decision: optimise for Windows, degrade rather than fail elsewhere.**

Dental practice management runs on Windows, and that is where the camera APIs
are. The USB and removable-media sources are Windows-specific and say so in
their probe; `folder` and `simulator` are pure Python and run anywhere. Sources
that cannot work on a given machine report *why* instead of crashing the
service, and `python run_bridge.py --check` prints exactly what a particular PC
can do before anyone relies on it.

No admin rights, no installer, no vendor driver required for the default path.

---

## Where the photographs live, and how they leave

**Decision: file the originals under the patient's own name, and treat that
folder — not the database — as the record.**

Everything lives under `DATA_DIR` (set in `backend/.env`, default `backend/data`):

```
backend/data/
├── snapchart.db                       SQLite: patients, sessions, image rows, audit log
├── photos/                            the originals, byte for byte
│   ├── <First>_<Last>_<Chart>/<YYYY-MM-DD>/<original camera filename>
│   └── _unassigned/<YYYY-MM-DD>/…     quarantine, until a human assigns it
└── thumbnails/<hh>/<sha256>.jpg       480 px previews, sharded by hash prefix
```

Two layouts on purpose. Originals are addressed **by person**, because the thing
a clinician does in a crisis is open a folder and look. Thumbnails are addressed
**by content hash**, because they are derived data: they dedupe on their own,
they never need to be found by hand, and keeping them out of the photo folders
means those folders contain nothing but real photographs.

The name comes first and the chart number last, because two patients genuinely
can share a name and a photograph in the wrong person's folder is the one
failure this system exists to prevent. An assignment out of quarantine *moves*
the file (`storage.move_image`) rather than leaving a pointer behind, so the
folder tree and the chart never disagree.

### Sharing

There are three ways a photograph leaves, and the first is the one that matters:

| Route | Mechanism |
| ----- | --------- |
| **The folder** | `GET /api/patients/{id}/folder` returns the path; the clinician opens it in Explorer and copies, attaches or burns it |
| **HTTP** | `GET /api/images/{id}/file` (original) and `/thumb` (preview) |
| **PMS** | not built — `/api/ingest` is the declared seam, see (6) |

Referring a case to a specialist is *copy one folder*. That works with this
software uninstalled, with the database deleted, on a machine that has never
heard of SnapChart. A content-addressed blob store would have been tidier and
would have thrown that away, which is the whole reason we did not build one.

Being straight about the limits, because they are the obvious questions:
`/api/images/{id}/file` is **unauthenticated** — the UUID is the only thing
guarding it, which is acceptable on loopback and thin once `-Lan` binds the
server to the practice network. There is no export, no share link, no expiry,
and the audit log records ingest and assignment but **not** retrieval.
Clinician identity (see *What we would do next*) is what closes all of these.

### Is the image still the image?

**Decision: never re-encode an original, and prove the bytes end to end.**

The backend writes the capture verbatim (`stored_path.write_bytes(data)`). Only
the thumbnail is re-compressed. SnapChart is therefore provably not a source of
quality loss — whatever the camera produced is what is on disk.

Integrity is checked by SHA-256, computed by the bridge before the upload and
recomputed by the backend before the insert; a mismatch is refused rather than
charted. That proves the bytes the bridge *held* are the bytes that were
*stored*, which covers the spool, the network hop and the multipart encoding.

It does not cover the read off the camera itself, because the hash is computed
after it. A stream that ends early without raising would be hashed in its
truncated form and agreed on by both sides. The device reports the object size
over MTP (`PID_OBJECT_SIZE`) and comparing it against the transferred length
closes that gap; see *What we would do next*.

---

## What we would do next

- Push the charted image into a real PMS document API behind `/api/ingest`.
- Clinician identity on the UI side (the bridge hop is a shared secret today).
  This is also what puts authentication in front of image retrieval and an
  access trail on reads, not only on writes.
- Verify the transfer at the point of the read, not only after it: compare the
  device-reported object size against the bytes actually transferred, reject a
  JPEG that does not open with `FFD8` and close with `FFD9`, and treat a failed
  decode as a rejection for JPEG/PNG rather than the silently-missing thumbnail
  it currently produces (RAW must keep its exemption — PIL cannot open it).
- Re-hash stored files against `images.content_hash` on a schedule, to catch bit
  rot, antivirus quarantine or a bad restore long after the capture.
- Per-image view classification — a five-shot series auto-labelled frontal,
  occlusal, buccal — using the capture order the simulator already models.
- Encryption at rest for the spool and image store.

## Known limitations

- The `wpd` USB path is verified end to end against a real MTP/PTP device over
  USB, but not against an actual DSLR body. The `shell` and Canon EDSDK transfer
  paths are exercised only through their unavailable/no-device branches.
- Device behaviour varies more than the WPD documentation implies. Hardware
  testing turned up three concrete divergences — counts must be requested via an
  explicit out-pointer, a storage root is a functional object rather than a
  folder, and date properties arrive in several shapes — all now handled, but a
  new body may well find a fourth. That is why `--check` exists and why two
  independent fallback transports are shipped rather than one.
- Nikon and Sony vendor adapters are unimplemented by design (transfer for those
  brands goes through `wpd`).
- Single-site assumptions throughout: SQLite, local files, one clinic.
