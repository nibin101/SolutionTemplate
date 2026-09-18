"""Bridge configuration, loaded once from environment / bridge/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BRIDGE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BRIDGE_DIR / ".env")


def _resolve(value: str) -> Path:
    """Expand a configured path and anchor relative ones to bridge/."""
    path = Path(os.path.expandvars(value.strip())).expanduser()
    return path if path.is_absolute() else (BRIDGE_DIR / path).resolve()


def _csv(name: str, default: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, default).split(",") if part.strip()]


def _port_range(value: str) -> tuple[int, int]:
    """Parse "50000-50100" into an inclusive (low, high) pair.

    A malformed value falls back to the default rather than stopping the bridge:
    a typo here must not take the whole capture service down with it.
    """
    try:
        low, _, high = value.strip().partition("-")
        first, last = int(low), int(high or low)
        if 1024 <= first <= last <= 65535:
            return first, last
    except ValueError:
        pass
    return 50000, 50100


def _normalise_extension(value: str) -> str:
    value = value.lower().strip()
    return value if value.startswith(".") else f".{value}"


@dataclass
class Config:
    backend_url: str = field(
        default_factory=lambda: os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    )
    bridge_token: str = field(default_factory=lambda: os.getenv("BRIDGE_TOKEN", "dev-token"))
    operatory: str = field(default_factory=lambda: os.getenv("OPERATORY", "OP-1"))

    enabled_sources: list[str] = field(
        default_factory=lambda: _csv("ENABLED_SOURCES", "wpd,folder,removable")
    )
    watch_folders: list[Path] = field(
        default_factory=lambda: [
            _resolve(p) for p in os.getenv("WATCH_FOLDERS", "./watch").split(";") if p.strip()
        ]
    )
    image_extensions: set[str] = field(
        default_factory=lambda: {
            _normalise_extension(e)
            for e in _csv(
                "IMAGE_EXTENSIONS",
                ".jpg,.jpeg,.png,.tif,.tiff,.heic,.cr2,.cr3,.nef,.arw",
            )
        }
    )
    poll_interval: float = field(
        default_factory=lambda: float(os.getenv("POLL_INTERVAL_SECONDS", "3"))
    )

    spool_dir: Path = field(default_factory=lambda: _resolve(os.getenv("SPOOL_DIR", "./spool")))
    max_upload_attempts: int = field(
        default_factory=lambda: int(os.getenv("MAX_UPLOAD_ATTEMPTS", "8"))
    )
    heartbeat_seconds: float = field(
        default_factory=lambda: float(os.getenv("HEARTBEAT_SECONDS", "5"))
    )

    simulator_folder: Path = field(
        default_factory=lambda: _resolve(os.getenv("SIMULATOR_FOLDER", "./sample-captures"))
    )
    simulator_interval: float = field(
        default_factory=lambda: float(os.getenv("SIMULATOR_INTERVAL_SECONDS", "6"))
    )

    # Wi-Fi cameras push straight to the bridge over FTP. 2121 rather than 21 so
    # the bridge never needs administrator rights to bind its port; every body
    # that speaks FTP lets you set the port alongside the address.
    ftp_host: str = field(default_factory=lambda: os.getenv("FTP_HOST", "0.0.0.0").strip())
    ftp_port: int = field(default_factory=lambda: int(os.getenv("FTP_PORT", "2121")))
    ftp_user: str = field(default_factory=lambda: os.getenv("FTP_USER", "camera").strip())
    #: No default on purpose - the source refuses to start without one.
    ftp_password: str = field(default_factory=lambda: os.getenv("FTP_PASSWORD", "").strip())
    ftp_root: Path = field(default_factory=lambda: _resolve(os.getenv("FTP_ROOT", "./ftp-incoming")))
    #: Data-channel ports for passive mode, which is what cameras use. Pinned to
    #: a small fixed range so the clinic firewall can be opened for exactly
    #: these; left to the OS it would be a random ephemeral port per transfer,
    #: and every transfer would stall at a closed port after connecting fine.
    ftp_passive_ports: tuple[int, int] = field(
        default_factory=lambda: _port_range(os.getenv("FTP_PASSIVE_PORTS", "50000-50100"))
    )

    canon_edsdk_dll: str = field(default_factory=lambda: os.getenv("CANON_EDSDK_DLL", "").strip())
    nikon_sdk_dll: str = field(default_factory=lambda: os.getenv("NIKON_SDK_DLL", "").strip())
    sony_sdk_dll: str = field(default_factory=lambda: os.getenv("SONY_SDK_DLL", "").strip())

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    @property
    def log_dir(self) -> Path:
        return BRIDGE_DIR / "logs"

    @property
    def ingest_url(self) -> str:
        return f"{self.backend_url}/api/ingest"

    @property
    def heartbeat_url(self) -> str:
        return f"{self.backend_url}/api/bridge/heartbeat"

    @property
    def ui_url(self) -> str:
        return f"{self.backend_url}/"

    def is_image(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in self.image_extensions


config = Config()
