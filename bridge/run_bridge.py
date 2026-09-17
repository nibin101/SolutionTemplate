"""SnapChart camera bridge - entry point.

    python run_bridge.py                 run in the foreground (logs to console)
    python run_bridge.py --tray          run in the background with a tray icon
    python run_bridge.py --check         probe every capture source and exit
    python run_bridge.py --install-autostart    start at login
    python run_bridge.py --remove-autostart     stop starting at login
"""

from __future__ import annotations

import argparse
import signal
import sys

from bridge.app import BridgeApp
from bridge.config import config
from bridge.logging_setup import setup_logging
from bridge.sources import BUILDERS, build_sources


def check_sources() -> int:
    """Report what each configured source can and cannot do on this machine."""
    print(f"Backend      : {config.backend_url}")
    print(f"Operatory    : {config.operatory}")
    print(f"Enabled      : {', '.join(config.enabled_sources)}")
    print(f"Known sources: {', '.join(sorted(BUILDERS))}")
    print()

    usable = 0
    for source in build_sources(config.enabled_sources):
        try:
            ok, detail = source.probe()
        except Exception as exc:
            ok, detail = False, f"probe raised {exc.__class__.__name__}: {exc}"
        usable += int(ok)
        print(f"  [{'OK ' if ok else '-- '}] {source.name:<10} {detail}")

    print()
    print(f"{usable} source(s) ready.")
    return 0 if usable else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SnapChart camera bridge")
    parser.add_argument("--tray", action="store_true",
                        help="run in the background with a system-tray icon")
    parser.add_argument("--check", action="store_true",
                        help="probe capture sources and exit")
    parser.add_argument("--install-autostart", action="store_true",
                        help="start the bridge automatically at login")
    parser.add_argument("--remove-autostart", action="store_true",
                        help="stop starting the bridge at login")
    args = parser.parse_args()

    if args.install_autostart or args.remove_autostart:
        from bridge import autostart

        if args.install_autostart:
            print(f"Autostart enabled:\n  {autostart.enable()}")
        else:
            print("Autostart removed." if autostart.disable() else "Autostart was not set.")
        return 0

    if args.check:
        return check_sources()

    setup_logging()
    app = BridgeApp()
    app.start()

    if args.tray:
        from bridge.tray import run_tray

        try:
            run_tray(app)          # blocks until the user picks Quit
        finally:
            app.stop()
        return 0

    # Foreground mode: Ctrl+C should drain cleanly rather than kill mid-upload.
    signal.signal(signal.SIGINT, lambda *_: app.stop_event.set())
    app.wait()
    app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
