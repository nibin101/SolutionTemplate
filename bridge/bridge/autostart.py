"""Start the bridge at login, without an installer or admin rights.

A per-user Run key is deliberate: a dental practice should not need a domain
admin to get the camera link working on one operatory PC, and an uninstall is a
single registry value. For a shared clinical workstation where the link must run
without anyone logging in, install the same entry point as a Windows service
instead - see bridge/README.md.
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SnapChartBridge"

BRIDGE_DIR = Path(__file__).resolve().parent.parent
ENTRY_POINT = BRIDGE_DIR / "run_bridge.py"


def launch_command() -> str:
    """The command written to the Run key.

    pythonw.exe rather than python.exe so login does not flash a console window
    on the clinician's screen every morning.
    """
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.exists():
        interpreter = windowless
    return f'"{interpreter}" "{ENTRY_POINT}" --tray'


def enable() -> str:
    command = launch_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    return command


def disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False


def current() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return value
    except FileNotFoundError:
        return None
