"""Image storage on disk, plus EXIF and thumbnail extraction.

Layout
------
One folder per person, named after them, filed where a human can find it
without the application:

    data/photos/Aarav_Menon_CS-1001/2026-09-18/IMG_0001.JPG
    data/photos/_unassigned/2026-09-18/IMG_0007.JPG
    data/thumbnails/a3/<sha256>.jpg

The name comes first because that is what someone scrolling a folder list is
looking for. The chart number stays on the end because two patients really can
share a name, and a photograph in the wrong person's folder is the one failure
this whole system exists to prevent.

A practice that has to hand records to a specialist, or that loses the software
entirely, still has an organised folder of clinical photographs. That is worth
more than the tidiness of a content-addressed blob store.

Deduplication does not depend on the path: the SHA-256 of the file bytes is
stored in the database with a unique index, and the caller checks it before
storing. Thumbnails stay content-addressed and out of the way, so the photo
folders contain only real photographs.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .config import settings

log = logging.getLogger(__name__)

THUMB_MAX_EDGE = 480

# Long-edge cap for the lightbox preview. 2048 is large enough to judge a
# clinical photograph on screen and small enough to cross a practice Wi-Fi
# instantly - a 24 MP original is 20-40x this size. An original already at or
# below this gets no preview at all: the file itself is the preview, and a
# near-identical second copy would be pure waste.
PREVIEW_MAX_EDGE = 2048
PREVIEW_QUALITY = 88

#: Where captures live until a human attaches them to a patient.
UNASSIGNED_FOLDER = "_unassigned"

# EXIF tag numbers we care about (see Exif 2.3 spec).
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_EXIF_IFD = 0x8769
_TAG_DATETIME_ORIGINAL = 0x9003

_EXTENSION_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/heic": ".heic",
}

# Windows rejects these outright; the rest is trimmed to keep folder names sane.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class StoredImage:
    content_hash: str
    stored_path: Path
    thumb_path: Path | None
    preview_path: Path | None
    mime: str
    size_bytes: int
    width: int | None
    height: int | None
    camera_make: str | None
    camera_model: str | None
    captured_at: str | None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Folder naming
# --------------------------------------------------------------------------


def safe_component(value: str, fallback: str = "unnamed") -> str:
    cleaned = _UNSAFE.sub("", value).strip().strip(".")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:60] or fallback


def patient_folder(chart_number: str, first_name: str, last_name: str) -> str:
    """The person's name, with their chart number to keep it unique."""
    return safe_component(f"{first_name}_{last_name}_{chart_number}", "patient")


def date_folder(captured_at: str | None) -> str:
    """One folder per day of capture - the unit a clinician actually thinks in."""
    if captured_at:
        try:
            moment = datetime.fromisoformat(captured_at)
        except ValueError:
            moment = datetime.now(timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    return moment.date().isoformat()


def folder_for(patient: dict | None, captured_at: str | None) -> str:
    """Relative folder a capture belongs in, given who it is for (if anyone)."""
    if patient is None:
        return f"{UNASSIGNED_FOLDER}/{date_folder(captured_at)}"
    name = patient_folder(
        patient["chart_number"], patient["first_name"], patient["last_name"]
    )
    return f"{name}/{date_folder(captured_at)}"


def _unique_path(directory: Path, filename: str) -> Path:
    """Never overwrite: two different photos may share a camera file name."""
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def _exif_datetime_to_iso(raw: str) -> str | None:
    """EXIF stores local time as 'YYYY:MM:DD HH:MM:SS' with no zone.

    Cameras rarely know their timezone, so we read it as local clock time and
    normalise to UTC using the machine's offset - the same assumption the
    clinician makes when they look at the camera's own clock.
    """
    try:
        naive = datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    return naive.astimezone().astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _read_metadata(image: Image.Image) -> tuple[str | None, str | None, str | None]:
    try:
        exif = image.getexif()
    except Exception:
        return None, None, None
    if not exif:
        return None, None, None

    make = exif.get(_TAG_MAKE)
    model = exif.get(_TAG_MODEL)
    captured = None
    try:
        sub_ifd = exif.get_ifd(_TAG_EXIF_IFD)
        if sub_ifd:
            captured = _exif_datetime_to_iso(sub_ifd.get(_TAG_DATETIME_ORIGINAL, ""))
    except Exception:
        captured = None

    def clean(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    return clean(make), clean(model), captured


# --------------------------------------------------------------------------
# Writing and moving
# --------------------------------------------------------------------------


def _write_preview(image: Image.Image, content_hash: str) -> Path | None:
    """Write the already-downscaled `image` as the content-addressed preview.

    Content-addressed and sharded like thumbnails, for the same reason: the
    patient folders must hold clinical photographs and nothing else. A failure
    here is not fatal - the caller falls back to serving the original.
    """
    shard = settings.preview_dir / content_hash[:2]
    shard.mkdir(parents=True, exist_ok=True)
    candidate = shard / f"{content_hash}.jpg"
    if candidate.exists():
        return candidate
    try:
        image.convert("RGB").save(
            candidate, "JPEG", quality=PREVIEW_QUALITY, optimize=True
        )
    except Exception:
        return None
    return candidate


def store_image(data: bytes, filename: str, mime: str, folder: str) -> StoredImage:
    """Write bytes into `folder` and derive a thumbnail plus camera metadata."""
    content_hash = sha256_hex(data)
    suffix = _EXTENSION_BY_MIME.get(mime) or (Path(filename).suffix.lower() or ".jpg")
    safe_name = safe_component(Path(filename).stem, content_hash[:12]) + suffix

    directory = settings.image_dir / folder
    directory.mkdir(parents=True, exist_ok=True)
    stored_path = _unique_path(directory, safe_name)
    stored_path.write_bytes(data)

    width = height = None
    make = model = captured_at = None
    thumb_path: Path | None = None
    preview_path: Path | None = None

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            make, model, captured_at = _read_metadata(image)

            # Decoded once, resampled once. `working` is the preview-sized
            # image when one is warranted, and the thumbnail is derived from
            # that rather than from the full-resolution original.
            oriented = ImageOps.exif_transpose(image)
            if max(oriented.size) > PREVIEW_MAX_EDGE:
                working = oriented.copy()
                working.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.LANCZOS)
                preview_path = _write_preview(working, content_hash)
            else:
                # Already small enough to hand straight to a browser; a second
                # near-identical copy would earn nothing.
                working = oriented

            thumb_shard = settings.thumb_dir / content_hash[:2]
            thumb_shard.mkdir(parents=True, exist_ok=True)
            candidate = thumb_shard / f"{content_hash}.jpg"
            if not candidate.exists():
                thumb = working.copy()
                thumb.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))
                thumb.convert("RGB").save(candidate, "JPEG", quality=82)
            thumb_path = candidate
    except Exception:
        # A RAW/unsupported file still gets stored and charted; it just has no
        # preview. Losing the clinical image would be far worse than losing a
        # thumbnail, so this failure is intentionally swallowed.
        thumb_path = None
        preview_path = None

    return StoredImage(
        content_hash=content_hash,
        stored_path=stored_path,
        thumb_path=thumb_path,
        preview_path=preview_path,
        mime=mime,
        size_bytes=len(data),
        width=width,
        height=height,
        camera_make=make,
        camera_model=model,
        captured_at=captured_at,
    )


# A file freshly written to a patient's day folder can be briefly held open by
# antivirus or the Windows search indexer scanning it - long enough that the
# very next request (a clinician assigning it within a second of it appearing)
# can lose the race. Retrying beats reporting a false success: a database row
# pointing at a file that is still where it was beats one pointing nowhere, but
# an assign that silently fails to move the file is worse than either.
_MOVE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


@dataclass
class MoveResult:
    path: Path
    #: True once the file is actually sitting in the target folder - whether it
    #: was moved there just now, or was already there. False only means the
    #: move was attempted and did not succeed.
    moved: bool
    error: str | None = None


def move_image(current: Path, folder: str) -> MoveResult:
    """Re-file a photograph when it is assigned to (or moved between) patients."""
    if not current.exists():
        log.warning("cannot move %s: source file is missing", current)
        return MoveResult(current, moved=False, error=f"source file missing: {current}")

    directory = settings.image_dir / folder
    directory.mkdir(parents=True, exist_ok=True)
    if current.parent == directory:
        return MoveResult(current, moved=True)

    destination = _unique_path(directory, current.name)
    last_error: OSError | None = None
    for attempt, delay in enumerate((0.0, *_MOVE_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            shutil.move(str(current), str(destination))
        except OSError as exc:
            last_error = exc
            log.debug("move attempt %d for %s failed: %s", attempt + 1, current.name, exc)
            continue
        else:
            last_error = None
            break

    if last_error is not None:
        log.error(
            "could not move %s into %s after %d attempt(s): %s",
            current.name, directory, len(_MOVE_RETRY_DELAYS) + 1, last_error,
        )
        return MoveResult(current, moved=False, error=str(last_error))

    # Leave the tree tidy: an emptied day folder is noise in a file listing.
    try:
        current.parent.rmdir()
    except OSError:
        pass
    return MoveResult(destination, moved=True)
