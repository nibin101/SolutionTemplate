"""
conftest.py — Shared pytest fixtures for all test modules.

Creates a fresh temporary directory structure for each test so tests are
fully isolated from each other and from the real inbox/outbox on disk.
"""

import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageFilter

# Allow imports from backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest import Config, QualityConfig, SessionEntry

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


# ── Quality-gate image fixtures (textured, so blur variance is meaningful) ──

def _checkerboard(width: int = 800, height: int = 600, cell: int = 50) -> Image.Image:
    """A sharp checkerboard — strong edges → high Laplacian variance."""
    import numpy as np

    arr = np.zeros((height, width), dtype=np.uint8)
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                arr[y : y + cell, x : x + cell] = 255
    return Image.fromarray(arr).convert("RGB")


@pytest.fixture()
def sharp_jpeg(tmp_dirs):
    """A sharp, high-detail image that must pass the blur check."""
    path = tmp_dirs["inbox"] / "JOHN_01_frontal_repose.jpg"
    _checkerboard().save(path, format="JPEG", quality=95)
    return path


@pytest.fixture()
def blurry_jpeg(tmp_dirs):
    """A heavily-blurred version of the sharp image — must fail blur."""
    path = tmp_dirs["inbox"] / "JOHN_02_frontal_blurry.jpg"
    _checkerboard().filter(ImageFilter.GaussianBlur(radius=10)).save(
        path, format="JPEG", quality=95
    )
    return path


@pytest.fixture()
def occlusal_jpeg(tmp_dirs):
    """
    A sharp image with NO face, named as an intraoral view.
    Face is not required for intraoral views → must still pass.
    """
    path = tmp_dirs["inbox"] / "JOHN_03_maxillary_occlusal.jpg"
    _checkerboard().save(path, format="JPEG", quality=95)
    return path


@pytest.fixture()
def face_jpeg():
    """
    A real frontal face photo (committed demo asset from the team, not patient
    data). Used for the positive face-detection test. Skipped automatically if
    the asset is missing.
    """
    path = Path(__file__).parent.parent / "demo_images" / "JOHN_01_frontal_repose.jpg"
    if not path.exists():
        pytest.skip("demo face image not available")
    return path


# ── Config fixture ─────────────────────────────────────────────────────────

@pytest.fixture()
def sample_config():
    """
    A minimal Config object with two sessions (one active, one inactive).

    The quality gate is DISABLED so core ingest tests focus on file handling.
    Use `quality_config` / `sample_config_quality` for gate behaviour tests.
    """
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
        quality=QualityConfig(enabled=False),
    )


@pytest.fixture()
def quality_config():
    """A QualityConfig with the gate fully enabled (default thresholds)."""
    return QualityConfig(
        enabled=True,
        analysis_max_px=512,
        blur_enabled=True,
        blur_min_variance=60.0,
        face_enabled=True,
        min_face_px=20,
    )


@pytest.fixture()
def sample_config_quality(sample_config):
    """sample_config but with the quality gate ENABLED."""
    sample_config.quality = QualityConfig(
        enabled=True,
        analysis_max_px=512,
        blur_enabled=True,
        blur_min_variance=60.0,
        face_enabled=True,
        min_face_px=20,
    )
    return sample_config
