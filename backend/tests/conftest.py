"""
conftest.py — Shared pytest fixtures for all test modules.

Creates a fresh temporary directory structure for each test so tests are
fully isolated from each other and from the real inbox/outbox on disk.
"""

import shutil
from pathlib import Path

import pytest
from PIL import Image

# ── Path fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_dirs(tmp_path):
    """Return a dict with all the working directories a test might need."""
    dirs = {
        "inbox":  tmp_path / "inbox",
        "outbox": tmp_path / "outbox",
        "manifest": tmp_path / "outbox" / ".manifest.json",
    }
    dirs["inbox"].mkdir(parents=True)
    dirs["outbox"].mkdir(parents=True)
    return dirs


# ── Sample image fixtures ──────────────────────────────────────────────────

def _make_jpeg(path: Path, width: int = 800, height: int = 600, with_exif: bool = True) -> Path:
    """
    Create a minimal JPEG file at the given path.

    If with_exif=True, embeds a DateTimeOriginal EXIF tag so the ingest
    pipeline has something to read.
    """
    img = Image.new("RGB", (width, height), color=(120, 60, 200))

    if with_exif:
        # Embed a minimal EXIF block with DateTimeOriginal
        import io
        import piexif

        exif_dict = {
            "0th": {},
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: b"2026:09:17 10:23:44",
            },
            "1st": {},
        }
        exif_bytes = piexif.dump(exif_dict)
        img.save(path, format="JPEG", quality=92, exif=exif_bytes)
    else:
        img.save(path, format="JPEG", quality=92)

    return path


@pytest.fixture()
def sample_jpeg(tmp_dirs):
    """A JPEG with EXIF DateTimeOriginal = 2026-09-17."""
    path = tmp_dirs["inbox"] / "JOHN_IMG_4823.jpg"
    try:
        _make_jpeg(path, with_exif=True)
    except ImportError:
        # piexif not installed — create without EXIF, tests use mtime fallback
        _make_jpeg(path, with_exif=False)
    return path


@pytest.fixture()
def sample_jpeg_no_exif(tmp_dirs):
    """A JPEG without any EXIF data (to test mtime fallback)."""
    path = tmp_dirs["inbox"] / "JANE_IMG_4824.jpg"
    _make_jpeg(path, with_exif=False)
    return path


@pytest.fixture()
def sample_jpeg_unmatched(tmp_dirs):
    """A JPEG whose filename doesn't match any session prefix."""
    path = tmp_dirs["inbox"] / "IMG_4825.jpg"
    _make_jpeg(path, with_exif=False)
    return path


# ── Config fixture ─────────────────────────────────────────────────────────

@pytest.fixture()
def sample_config():
    """A minimal Config object with two sessions (one active, one inactive)."""
    import sys, os
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from ingest import Config, SessionEntry

    return Config(
        practice_id="TEST-001",
        sessions=[
            SessionEntry(
                patient_id="P001",
                patient_name="Smith, John",
                session_date="2026-09-17",
                filename_prefix="JOHN_",
                active=True,
            ),
            SessionEntry(
                patient_id="P002",
                patient_name="Doe, Jane",
                session_date="2026-09-17",
                filename_prefix="JANE_",
                active=False,  # inactive — should NOT match
            ),
        ],
        default_patient_id="UNMATCHED",
        default_patient_name="Unmatched Patient",
        preview_max_px=256,    # small for fast tests
        preview_quality=80,
        supported_extensions=[".jpg", ".jpeg", ".cr2", ".nef"],
    )
