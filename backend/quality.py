"""
quality.py — Computer-vision quality gate for the dental camera workflow.

Every new image is screened before it is accepted into the patient archive:

  1. Blur check — Laplacian variance via OpenCV. A low variance means there are
     few high-frequency edges, i.e. the photo is soft / out of focus. Sharp
     clinical photos score high; solid-colour or motion-blurred images score
     near zero.
  2. Face check — OpenCV Haar cascade (haarcascade_frontalface_default.xml)
     counts frontal faces. The check is only *enforced* for extraoral views
     (frontal face, smile, repose) because intraoral shots (occlusal, buccal,
     anterior, mirror views) legitimately contain no face at all.

All analysis runs on a downscaled, in-memory copy of the image — the original
file is never decoded back to disk, re-encoded, or modified. Downscaling to a
fixed analysis size also keeps the blur threshold resolution-independent.

Both techniques are classic, offline (no model download, no API keys), and
easy to explain in a technical review.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from preview import decode_source

logger = logging.getLogger(__name__)

# Haar cascade shipped inside opencv-python-headless — no download needed.
_DEFAULT_CASCADE = "haarcascade_frontalface_default.xml"

# Filename keywords used to decide whether a face is expected.
_DEFAULT_FACE_REQUIRED_KEYWORDS = ("frontal", "smile", "repose", "face", "extraoral")
_DEFAULT_FACE_OPTIONAL_KEYWORDS = ("profile",)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class QualityConfig:
    """Thresholds and rules for the quality gate (config.json → "quality")."""
    enabled: bool = True
    analysis_max_px: int = 512                 # longest edge for analysis copy
    blur_enabled: bool = True
    blur_min_variance: float = 60.0            # below this → rejected as blurry
    face_enabled: bool = True
    face_required_keywords: tuple[str, ...] = _DEFAULT_FACE_REQUIRED_KEYWORDS
    face_optional_keywords: tuple[str, ...] = _DEFAULT_FACE_OPTIONAL_KEYWORDS
    min_face_px: int = 20                      # minSize for the haar detector


@dataclass
class QualityResult:
    """Outcome of running the quality gate on a single image."""
    passed: bool
    blur_score: float = 0.0
    blur_rejected: bool = False
    faces_detected: int = 0
    face_required: bool = False
    face_rejected: bool = False
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# View classification (which checks apply to which photo)
# ---------------------------------------------------------------------------

def classify_view(filename: str, cfg: QualityConfig) -> str:
    """
    Decide whether a face is expected for this image based on its filename.

    Returns one of:
      "face_required"  → extraoral shot, a face must be present
      "face_optional"  → we look for a face but never reject on its absence
      "no_face"        → intraoral / detail shot, face not applicable
    """
    upper = filename.upper()
    if any(kw.upper() in upper for kw in cfg.face_required_keywords):
        return "face_required"
    if any(kw.upper() in upper for kw in cfg.face_optional_keywords):
        return "face_optional"
    return "no_face"


# ---------------------------------------------------------------------------
# OpenCV primitives
# ---------------------------------------------------------------------------

def _to_gray_array(img: Image.Image, max_px: int) -> np.ndarray:
    """Resize to <= max_px on the longest edge and return a grayscale array."""
    if max(int(max(img.size)), 1) > max_px:
        img.thumbnail((max_px, max_px), resample=Image.LANCZOS)
    return np.asarray(img.convert("L"))


def measure_blur(gray: np.ndarray) -> float:
    """
    Blur score = variance of the image Laplacian.

    A sharp image has many strong edges → high variance. A blurry/flat image
    has few → near zero. Thresholds scale with the analysis size, so we always
    analyse at AnalysisConfig.analysis_max_px.
    """
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def detect_faces(gray: np.ndarray, min_face_px: int = 20) -> int:
    """
    Count frontal faces using OpenCV's bundled Haar cascade.

    Returns the number of detected faces. Haar is heuristic (not learned on
    patient data) and runs entirely offline.
    """
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + _DEFAULT_CASCADE
    )
    if cascade.empty():
        logger.warning("Face cascade failed to load — face check disabled")
        return 0
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_face_px, min_face_px)
    )
    return len(faces)


# ---------------------------------------------------------------------------
# Full gate
# ---------------------------------------------------------------------------

def assess_image(source: Path, cfg: QualityConfig) -> QualityResult:
    """
    Run the blur and (if applicable) face checks on an image.

    Never touches the source file — decodes a downscaled in-memory copy only.
    Returns a QualityResult describing whether the image passed and why not.
    """
    reasons: list[str] = []

    try:
        img = decode_source(source)
    except Exception as exc:
        logger.warning("Quality gate could not read %s: %s", source.name, exc)
        return QualityResult(
            passed=False,
            reasons=[f"Could not analyse image — please re-send the file ({exc})"],
        )

    gray = _to_gray_array(img, cfg.analysis_max_px)

    blur_score = 0.0
    blur_rejected = False
    if cfg.blur_enabled:
        blur_score = measure_blur(gray)
        if blur_score < cfg.blur_min_variance:
            blur_rejected = True
            reasons.append(
                f"Photo is blurry (variance {round(blur_score, 1)} "
                f"< {cfg.blur_min_variance})"
            )

    faces_detected = 0
    face_required = False
    face_rejected = False
    if cfg.face_enabled:
        view = classify_view(source.name, cfg)
        if view in ("face_required", "face_optional"):
            face_required = view == "face_required"
            faces_detected = detect_faces(gray, cfg.min_face_px)
            if view == "face_required" and faces_detected == 0:
                face_rejected = True
                reasons.append("No face detected in frame — please retake")

    return QualityResult(
        passed=not reasons,
        blur_score=round(blur_score, 1),
        blur_rejected=blur_rejected,
        faces_detected=faces_detected,
        face_required=face_required,
        face_rejected=face_rejected,
        reasons=reasons,
    )