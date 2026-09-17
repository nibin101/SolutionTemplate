"""Capture source registry.

Names here are exactly what goes in ENABLED_SOURCES in bridge/.env. Builders
import lazily so that a station missing an optional dependency (comtypes,
pywin32) still starts with the sources it can actually run.
"""

from __future__ import annotations

from typing import Callable

from ..logging_setup import setup_logging
from .base import CaptureSource

log = setup_logging().getChild("sources")


def _folder() -> CaptureSource:
    from .folder_watch import FolderWatchSource

    return FolderWatchSource()


def _removable() -> CaptureSource:
    from .removable_media import RemovableMediaSource

    return RemovableMediaSource()


def _wpd() -> CaptureSource:
    from .wpd_ptp import WpdPtpSource

    return WpdPtpSource()


def _shell() -> CaptureSource:
    from .shell_mtp import ShellMtpSource

    return ShellMtpSource()


def _simulator() -> CaptureSource:
    from .simulator import SimulatorSource

    return SimulatorSource()


def _canon() -> CaptureSource:
    from .vendor.canon_edsdk import CanonEdsdkSource

    return CanonEdsdkSource()


def _nikon() -> CaptureSource:
    from .vendor.nikon_sdk import NikonSdkSource

    return NikonSdkSource()


def _sony() -> CaptureSource:
    from .vendor.sony_sdk import SonySdkSource

    return SonySdkSource()


BUILDERS: dict[str, Callable[[], CaptureSource]] = {
    "wpd": _wpd,
    "shell": _shell,
    "folder": _folder,
    "removable": _removable,
    "simulator": _simulator,
    "canon": _canon,
    "nikon": _nikon,
    "sony": _sony,
}


def build_sources(names: list[str]) -> list[CaptureSource]:
    sources: list[CaptureSource] = []
    for name in names:
        builder = BUILDERS.get(name.lower())
        if builder is None:
            log.warning("unknown source %r - known: %s", name, ", ".join(sorted(BUILDERS)))
            continue
        try:
            sources.append(builder())
        except Exception as exc:
            # A source that cannot even be constructed (missing dependency)
            # must not stop the others from running.
            log.error("could not create source %r: %s", name, exc)
    return sources


__all__ = ["BUILDERS", "CaptureSource", "build_sources"]
