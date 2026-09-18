"""
ingest.py — Core ingestion pipeline for the dental camera workflow.

Responsibilities:
  - Compute SHA-256 hash of an incoming image file
  - Check for duplicates against a persistent manifest
  - Extract capture date from EXIF metadata
  - Resolve patient session from filename prefix
  - Copy the original file byte-for-bit (no decode/re-encode = no quality loss)
  - Update the manifest with the newly processed file
  - Emit a structured IngestResult for the API and dashboard

All functions are pure / side-effect-free where possible so they are easy to
unit-test without a camera or a live CareStack account.
"""

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import exifread

from quality import QualityConfig, QualityResult, assess_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SessionEntry:
    """One row from config.json → sessions[]."""
    patient_id: str
    patient_name: str
    session_date: str
    filename_prefix: str
    active: bool


@dataclass
class Config:
    """Parsed config.json."""
    practice_id: str
    sessions: list[SessionEntry]
    default_patient_id: str
    default_patient_name: str
    preview_max_px: int
    preview_quality: int
    supported_extensions: list[str]
    quality: QualityConfig = field(default_factory=QualityConfig)


@dataclass
class IngestResult:
    """Outcome of processing a single image file."""
    filename: str
    status: str                    # "ingested" | "duplicate" | "error" | "skipped" | "rejected"
    patient_id: str = ""
    patient_name: str = ""
    capture_date: str = ""
    original_dest: str = ""
    preview_dest: str = ""
    carestack_status: str = ""     # "uploaded" | "mock" | "error" | "pending"
    sha256: str = ""
    error_message: str = ""
    quality_score: float = 0.0     # blur variance from the quality gate
    quality_faces: int = 0         # faces detected by the quality gate
    quality_reasons: list[str] = field(default_factory=list)
    processed_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> Config:
    """Parse config.json into a Config object."""
    with open(config_path, "r") as fh:
        raw = json.load(fh)

    sessions = [
        SessionEntry(
            patient_id=s["patient_id"],
            patient_name=s["patient_name"],
            session_date=s["session_date"],
            filename_prefix=s["filename_prefix"],
            active=s.get("active", False),
        )
        for s in raw.get("sessions", [])
    ]

    q = raw.get("quality", {}) or {}
    quality = QualityConfig(
        enabled=q.get("enabled", True),
        analysis_max_px=int(q.get("analysis_max_px", 512)),
        blur_enabled=q.get("blur_enabled", True),
        blur_min_variance=float(q.get("blur_min_variance", 60.0)),
        face_enabled=q.get("face_enabled", True),
        face_required_keywords=tuple(
            q.get(
                "face_required_keywords",
                ["frontal", "smile", "repose", "face", "extraoral"],
            )
        ),
        face_optional_keywords=tuple(
            q.get("face_optional_keywords", ["profile"])
        ),
        min_face_px=int(q.get("min_face_px", 20)),
    )

    return Config(
        practice_id=raw.get("practice_id", ""),
        sessions=sessions,
        default_patient_id=raw.get("default_patient_id", "UNMATCHED"),
        default_patient_name=raw.get("default_patient_name", "Unmatched Patient"),
        preview_max_px=raw.get("preview_max_px", 2048),
        preview_quality=raw.get("preview_quality", 92),
        supported_extensions=[
            ext.lower() for ext in raw.get("supported_extensions", [".jpg", ".jpeg"])
        ],
        quality=quality,
    )


# ---------------------------------------------------------------------------
# Manifest (duplicate detection)
# ---------------------------------------------------------------------------

def load_manifest(manifest_path: Path) -> dict:
    """
    Load the SHA-256 → metadata manifest from disk.
    Returns an empty dict if the file does not exist yet.
    """
    if not manifest_path.exists():
        return {}
    with open(manifest_path, "r") as fh:
        return json.load(fh)


def save_manifest(manifest_path: Path, manifest: dict) -> None:
    """Persist the manifest atomically by writing to a temp file then renaming."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2)
    tmp.replace(manifest_path)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def compute_sha256(file_path: Path, chunk_size: int = 65536) -> str:
    """
    Compute the SHA-256 digest of a file without loading it fully into memory.
    chunk_size of 64 KB is a good balance for large RAW files (20–50 MB).
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_duplicate(sha256: str, manifest: dict) -> bool:
    """Return True if this hash has already been ingested."""
    return sha256 in manifest


# ---------------------------------------------------------------------------
# EXIF extraction
# ---------------------------------------------------------------------------

# Raw file extensions that exifread can handle directly
_RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2"}


def extract_capture_date(file_path: Path) -> str:
    """
    Extract the DateTimeOriginal EXIF tag and return it as a YYYY-MM-DD string.
    Falls back to the file's mtime if EXIF is absent or unreadable.

    exifread supports JPEG and all major RAW formats (CR2, CR3, NEF, ARW, etc.)
    without decoding the image — it reads only the metadata header.
    """
    try:
        with open(file_path, "rb") as fh:
            tags = exifread.process_file(fh, stop_tag="EXIF DateTimeOriginal", details=False)

        dto_tag = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
        if dto_tag:
            # EXIF datetime format: "2026:09:17 10:23:44"
            dt_str = str(dto_tag)
            dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")
    except Exception as exc:
        logger.warning("EXIF read failed for %s: %s — falling back to mtime", file_path.name, exc)

    # Fallback: use file modification time (usually the capture transfer time)
    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
    return mtime.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Patient / session resolution
# ---------------------------------------------------------------------------

def resolve_patient(filename: str, config: Config) -> SessionEntry:
    """
    Match a filename against active session prefix rules.

    Only sessions with active=True are considered. The first prefix match wins.
    If no active session matches, returns a fallback SessionEntry using
    config.default_patient_id so files are never silently lost.
    """
    upper_filename = filename.upper()
    for session in config.sessions:
        if session.active and upper_filename.startswith(session.filename_prefix.upper()):
            return session

    # No prefix match — use the default session (UNMATCHED bucket)
    return SessionEntry(
        patient_id=config.default_patient_id,
        patient_name=config.default_patient_name,
        session_date=datetime.now().strftime("%Y-%m-%d"),
        filename_prefix="",
        active=True,
    )


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def build_output_paths(
    patient_id: str,
    patient_name: str,
    capture_date: str,
    source_path: Path,
    outbox_root: Path,
) -> tuple[Path, Path]:
    """
    Compute destination paths for the original file and its preview.

    Layout:
        outbox/{patient_id}_{safe_name}/{capture_date}/{original_filename}
        outbox/{patient_id}_{safe_name}/{capture_date}/{stem}_preview.jpg
    """
    safe_name = patient_name.replace(", ", "_").replace(" ", "_").replace("/", "_")
    folder_name = f"{patient_id}_{safe_name}"
    patient_dir = outbox_root / folder_name / capture_date
    patient_dir.mkdir(parents=True, exist_ok=True)

    original_dest = patient_dir / source_path.name
    preview_dest = patient_dir / f"{source_path.stem}_preview.jpg"
    return original_dest, preview_dest


def copy_original(source: Path, destination: Path) -> None:
    """
    Copy the original image byte-for-bit to the outbox.

    shutil.copy2 preserves file metadata (timestamps, permissions).
    The file is NEVER opened, decoded, or re-encoded — this is the guarantee
    of zero quality loss to the original.
    """
    shutil.copy2(source, destination)
    logger.info("Original copied: %s → %s", source.name, destination)


def is_supported_extension(file_path: Path, config: Config) -> bool:
    """Return True if the file extension is in the supported list."""
    return file_path.suffix.lower() in config.supported_extensions


# ---------------------------------------------------------------------------
# Full pipeline orchestrator
# ---------------------------------------------------------------------------

def process_file(
    source_path: Path,
    config: Config,
    manifest: dict,
    outbox_root: Path,
) -> IngestResult:
    """
    Run the full ingestion pipeline for a single image file.

    Steps:
      1. Check extension is supported
      2. Compute SHA-256
      3. Check for duplicate
      4. Extract EXIF capture date
      5. Resolve patient from filename prefix
      6. Build output paths
      7. Copy original (byte-for-bit, no quality loss)
      8. Update manifest

    Returns an IngestResult describing the outcome. Preview generation and
    CareStack upload are handled by the caller (main.py) asynchronously so
    this function stays synchronous and easily testable.
    """
    filename = source_path.name

    # Step 1 — Extension check
    if not is_supported_extension(source_path, config):
        return IngestResult(
            filename=filename,
            status="skipped",
            error_message=f"Unsupported extension: {source_path.suffix}",
        )

    # Step 2 — Hash
    try:
        sha256 = compute_sha256(source_path)
    except OSError as exc:
        return IngestResult(filename=filename, status="error", error_message=str(exc))

    # Step 3 — Duplicate check
    if is_duplicate(sha256, manifest):
        logger.info("Duplicate skipped: %s (sha256=%s…)", filename, sha256[:8])
        return IngestResult(
            filename=filename,
            status="duplicate",
            sha256=sha256,
            patient_id=manifest[sha256].get("patient_id", ""),
            patient_name=manifest[sha256].get("patient_name", ""),
        )

    # Step 3.5 — Quality gate (blur + face detection)
    # Rejected images are not stored, not logged to the manifest, and never
    # uploaded — the dashboard surfaces the reason so the photo is retaken.
    quality = QualityResult(passed=True)
    if config.quality.enabled:
        quality = assess_image(source_path, config.quality)
        if not quality.passed:
            reason_text = "; ".join(quality.reasons)
            logger.info("Quality gate rejected %s: %s", filename, reason_text)
            return IngestResult(
                filename=filename,
                status="rejected",
                sha256=sha256,
                error_message=reason_text,
                quality_score=quality.blur_score,
                quality_faces=quality.faces_detected,
                quality_reasons=quality.reasons,
            )

    # Step 4 — EXIF date
    capture_date = extract_capture_date(source_path)

    # Step 5 — Patient resolution
    session = resolve_patient(filename, config)

    # Step 6 — Output paths
    original_dest, preview_dest = build_output_paths(
        patient_id=session.patient_id,
        patient_name=session.patient_name,
        capture_date=capture_date,
        source_path=source_path,
        outbox_root=outbox_root,
    )

    # Step 7 — Copy original
    try:
        copy_original(source_path, original_dest)
    except OSError as exc:
        return IngestResult(
            filename=filename,
            status="error",
            sha256=sha256,
            patient_id=session.patient_id,
            patient_name=session.patient_name,
            error_message=f"Copy failed: {exc}",
        )

    # Step 8 — Update manifest
    manifest[sha256] = {
        "filename": filename,
        "patient_id": session.patient_id,
        "patient_name": session.patient_name,
        "capture_date": capture_date,
        "original_dest": str(original_dest),
        "processed_at": datetime.now().isoformat(),
    }

    return IngestResult(
        filename=filename,
        status="ingested",
        sha256=sha256,
        patient_id=session.patient_id,
        patient_name=session.patient_name,
        capture_date=capture_date,
        original_dest=str(original_dest),
        preview_dest=str(preview_dest),
        carestack_status="pending",
        quality_score=quality.blur_score,
        quality_faces=quality.faces_detected,
        quality_reasons=quality.reasons,
    )
