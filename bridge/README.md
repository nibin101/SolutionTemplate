# Camera Bridge — SnapChart

The background service that runs on the operatory PC. It watches for new
photographs from whatever camera is attached, and pushes them to the SnapChart
backend. No clinician interaction, no file copying, no folder wrangling.

## Stack

- Language / runtime: Python 3.12
- Camera access: Windows Portable Devices (MTP/PTP) via `comtypes`, Windows
  Shell via `pywin32`, `watchdog` for folder sources, `ctypes` for vendor SDKs
- Background UI: `pystray` tray icon
- Outbound: `requests` against the backend's `/api/ingest`

## Local Setup

```powershell
cd bridge
..\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then set BRIDGE_TOKEN to match backend\.env
..\.venv\Scripts\python run_bridge.py --check     # what can this PC actually do?
..\.venv\Scripts\python run_bridge.py             # run in the foreground
```

`scripts\setup.ps1` at the repo root does all of the above in one step.

## Running it as a background app

| Command | What it does |
|---------|--------------|
| `python run_bridge.py` | Foreground, logs to the console. Use while developing. |
| `python run_bridge.py --tray` | Background with a tray icon: status colour, pause, retry, open chart view, quit. |
| `python run_bridge.py --install-autostart` | Starts the bridge (tray mode, no console window) at every login. |
| `python run_bridge.py --remove-autostart` | Undoes that. |
| `python run_bridge.py --check` | Probes every configured source and prints what is usable. |

Autostart writes a single per-user `HKCU\...\CurrentVersion\Run` value, so no
installer and no admin rights are needed. For a shared workstation that must
capture with nobody logged in, wrap the same entry point with a service host
such as NSSM (`nssm install SnapChartBridge <venv>\Scripts\pythonw.exe
<repo>\bridge\run_bridge.py`) — the code path is identical, only the lifetime
differs.

## Capture sources

Set `ENABLED_SOURCES` in `.env`. All enabled sources run at once; every one of
them feeds the same spool, so mixing them is safe.

| Name | Transport | Use it when |
|------|-----------|-------------|
| `wpd` | USB MTP/PTP through Windows Portable Devices | **Default for DSLRs.** One implementation covers Canon, Nikon and Sony with no vendor SDK. |
| `shell` | USB MTP through the Windows Shell | A body that will not transfer over `wpd`. |
| `folder` | Filesystem watch | Vendor tether software (EOS Utility, NX Tether, Imaging Edge) or camera Wi-Fi/FTP drop. |
| `ftp` | Built-in FTP server | **Wi-Fi cameras.** Canon R / Nikon Z / Sony Alpha / Fujifilm push each frame straight to the bridge, no vendor software and nothing on the camera. |
| `removable` | SD card / mass-storage | Clinician brings the card to a reader. Imports the last 12 hours of DCIM. |
| `simulator` | Synthetic | Demo and tests with no hardware attached. |
| `canon` | Canon EDSDK | You have EDSDK and want push-on-shutter instead of polling. |
| `nikon` / `sony` | Vendor SDKs | Not implemented — see the module docstrings for why, and what they would add. |

### Wi-Fi cameras (`ftp`)

Every current body with wireless can FTP each frame as it is shot; it is the one
wireless path Canon, Nikon, Sony and Fujifilm all agree on. Enabling `ftp` makes
the bridge *be* that server, so nothing else has to be installed or kept running:

```ini
ENABLED_SOURCES=wpd,folder,removable,ftp
FTP_PORT=2121
FTP_USER=camera
FTP_PASSWORD=<something long>
FTP_PASSIVE_PORTS=50000-50100
```

Both devices must be on the **same local network** - the same router, by Wi-Fi
or cable. "Both online" is not enough: a camera on a phone hotspot and a PC on
the practice Wi-Fi can both reach the internet and still have no route to each
other. Guest and "client isolation" networks block device-to-device traffic by
design, so put the camera on the normal network, not the guest SSID.

Open the firewall once, on the practice PC, in an Administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "SnapChart FTP control" -Direction Inbound `
  -Protocol TCP -LocalPort 2121 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "SnapChart FTP data" -Direction Inbound `
  -Protocol TCP -LocalPort 50000-50100 -Action Allow -Profile Private
```

Two rules, not one, because FTP uses two connections: the camera opens the
control channel to `FTP_PORT`, then the *data* for each photo crosses a second
connection on a port from `FTP_PASSIVE_PORTS`. Opening only the first is the
classic failure - the camera reports a successful login and every transfer then
times out. `-Profile Private` keeps it off any public network the laptop joins
later; make sure the practice Wi-Fi is set to Private in Windows.

Then in the camera's network menu, point FTP transfer at this PC's LAN address
(`ipconfig` → IPv4 Address) and that port, with those credentials. Use passive
mode if the camera offers the choice. Frames arrive in about a second and go
through the same dedupe, spool, retry and charting pipeline as a USB capture.

Quick check from any other machine on the network, before involving the camera:

```powershell
Test-NetConnection <pc-ip> -Port 2121    # TcpTestSucceeded : True
```

Port 2121 rather than 21 so the bridge never needs administrator rights; every
body that speaks FTP lets you set the port next to the address. The source
refuses to start without a password - an anonymous drop box on a practice
network is not acceptable - and the landing directory is drained as fast as
files arrive, so the bridge never becomes a second unmanaged pile of patient
photographs.

Preferred over pointing `folder` at an SMB share: a folder watcher has to guess
when a file is finished, while FTP is *told* by the protocol, so a photo can
never be read half-written.

### Why MTP/PTP is the primary path

Canon EDSDK, the Nikon SDK and Sony's Camera Remote SDK are three different
registration-walled native libraries. Every one of those bodies also speaks PTP
over USB, and Windows exposes PTP through one vendor-neutral COM API. Targeting
that gets all three brands from a single implementation, with nothing to license
and no extra driver on a clinic PC. The vendor adapters stay available for what
only they can do — remote shutter, live view — as an enhancement, not a
dependency.

## How the bridge decides what to take

There is one rule, and it is the whole design:

> **A photograph is transferred if it was taken between *Start capture* and
> *Stop*. Nothing else is touched.**

The bridge does not ask "what is new on the camera?", because the answer to that
question includes somebody's holiday photographs. It asks "what was taken while
the patient was in the chair?" — and the camera's own timestamp answers it.

The window comes down on the heartbeat reply. The backend is the only side that
knows which patient is in which room, so it sends two timestamps and nothing
else: **the bridge is told *when*, never *who*.** Patient identity never reaches
the capture PC, and there is no command channel and no inbound port — a clinic
PC accepts no connections.

Each file on the device gets one of three verdicts:

| Verdict | When | What happens |
|---------|------|--------------|
| **Take** | Inside the window | Transferred and charted. |
| **Skip** | Taken before the session started, or undated | Marked seen, never read again. |
| **Wait** | Taken after the window closed | Left alone — it may belong to the *next* patient. |

Two details that came out of real hardware:

- **90 seconds of slack** either side (`CLOCK_SKEW`). Camera clocks drift, and a
  body whose clock was never set can be minutes out. Too small to reach into the
  next appointment; big enough that an unset clock does not lose a real shot.
- **An undated photograph is never charted.** If a device reports no usable
  timestamp we cannot show the photo belongs to this patient, and an unprovable
  photograph in a dental record is worse than a missing one.

Plugging a phone in therefore imports *nothing*: every photo already on it was
taken while nobody was in the chair. That is not a setting — it falls out of the
rule.

## Reliability

Every capture is written to `spool/` **before** any upload is attempted, and only
removed once the backend has confirmed it. That gives:

- **Power loss / crash** — the queue is on disk; it drains on restart.
- **Backend down** — exponential backoff with jitter, capped at 5 minutes.
- **Duplicates** — SHA-256 of the file bytes, checked in the bridge (never queue
  the same image twice) and again in the backend (never chart it twice).
- **Corruption in transit** — the hash travels with the upload and the backend
  refuses a mismatch.
- **Give-up** — after `MAX_UPLOAD_ATTEMPTS` an item moves to a *failed* bucket
  with its bytes intact, visible in the tray and the chair-side UI. Nothing is
  ever silently dropped.

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `BACKEND_URL` | Backend base URL | `http://127.0.0.1:8000` |
| `BRIDGE_TOKEN` | Shared secret; must match `backend/.env` | `a-long-random-string` |
| `OPERATORY` | Room this PC is in — how captures find the right patient | `OP-1` |
| `ENABLED_SOURCES` | Comma-separated source names | `wpd,folder,removable` |
| `WATCH_FOLDERS` | Semicolon-separated folders for the `folder` source | `C:\Tether;D:\DCIM` |
| `IMAGE_EXTENSIONS` | File types treated as clinical images | `.jpg,.cr3,.nef,.arw` |
| `POLL_INTERVAL_SECONDS` | USB / removable poll cadence — how long a shot can sit on the camera | `3` |
| `SPOOL_DIR` | Durable outbound queue | `./spool` |
| `MAX_UPLOAD_ATTEMPTS` | Retries before an item is parked as failed | `8` |
| `HEARTBEAT_SECONDS` | Liveness cadence, and how fast the capture window refreshes | `5` |
| `SIMULATOR_FOLDER` | Images replayed by the `simulator` source | `./sample-captures` |
| `CANON_EDSDK_DLL` | Path to `EDSDK.dll` (optional) | `C:\EDSDK\Dll\EDSDK.dll` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` | `INFO` |

> Never commit real `.env` values — see the root `.gitignore`. A starting point
> is provided in `.env.example`.

## Tests

```powershell
..\.venv\Scripts\python -m pytest tests -q
```

Covers the spool's durability and dedupe guarantees, the uploader's retry and
duplicate handling (with a stubbed transport, no network), the simulator, and a
real end-to-end run of the folder watcher against a temporary directory.

**Verified on hardware:** the `wpd` path has been run end to end against a real
MTP/PTP device over USB — enumeration, device naming, tree walk, byte transfer
and charting. Date parsing for the formats real devices actually emit is covered
by [tests/test_wpd_dates.py](tests/test_wpd_dates.py).

**Not covered by tests:** the `shell` and `canon` transfer paths, and `wpd`
against an actual DSLR body rather than a phone. Their *unavailable* paths are
tested; their transfer paths are not. Run `python run_bridge.py --check` on the
target machine to see what that machine reports before trusting it.

## Project Layout

```
bridge/
├── run_bridge.py            entry point (--tray, --check, --install-autostart)
├── bridge/
│   ├── app.py               supervisor: source threads, uploader, heartbeat
│   ├── spool.py             durable queue, dedupe, retry/back-off
│   ├── uploader.py          the only component that touches the network
│   ├── tray.py              background tray icon and menu
│   ├── autostart.py         start-at-login registration
│   ├── config.py            .env loading
│   └── sources/
│       ├── base.py          the CaptureSource contract
│       ├── wpd_ptp.py       USB MTP/PTP (Canon/Nikon/Sony)
│       ├── shell_mtp.py     USB MTP fallback
│       ├── folder_watch.py  tether / Wi-Fi drop folders
│       ├── removable_media.py  SD cards and mass-storage cameras
│       ├── simulator.py     synthetic camera for demo and tests
│       └── vendor/          Canon EDSDK, Nikon, Sony adapters
├── tests/
└── .env.example
```
