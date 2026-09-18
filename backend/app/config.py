"""Backend configuration, loaded once from environment / backend/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent

load_dotenv(BACKEND_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bridge_token: str
    data_dir: Path
    host: str
    port: int
    serve_frontend: bool
    quarantine_unassigned: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "snapchart.db"

    @property
    def image_dir(self) -> Path:
        """Root of the browsable, per-patient photo tree."""
        return self.data_dir / "photos"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def preview_dir(self) -> Path:
        """Mid-size JPEGs for the lightbox, so opening a photo never pulls the
        full-resolution original across the practice network."""
        return self.data_dir / "previews"

    @property
    def frontend_dir(self) -> Path:
        return REPO_DIR / "frontend" / "public"


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = (BACKEND_DIR / data_dir).resolve()

    settings = Settings(
        bridge_token=os.getenv("BRIDGE_TOKEN", "dev-token"),
        data_dir=data_dir,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        serve_frontend=_bool("SERVE_FRONTEND", True),
        quarantine_unassigned=_bool("QUARANTINE_UNASSIGNED", True),
    )
    settings.image_dir.mkdir(parents=True, exist_ok=True)
    settings.thumb_dir.mkdir(parents=True, exist_ok=True)
    settings.preview_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = load_settings()
