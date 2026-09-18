"""Computer-vision quality gate for incoming captures.

Ported from the `wot` branch's standalone MVP (`backend/quality.py` there),
adapted to master's architecture in two ways:

* It runs on the bytes already in memory (`assess_image(data, ...)`), not on a
  path re-read from disk. `chart_capture` already holds the decoded image in
  memory for EXIF/thumbnail work; the quality gate reuses that instead of a
  second file round trip, and never touches the source file.
* View classification prefers an explicit `view` hint (the bridge simulator
  already tags each shot - "Frontal retracted", "Right buccal" - but the field
  was previously dropped before reaching the backend; see bridge/uploader.py)
  over guessing from the filename. A real camera's filename (`IMG_0001.JPG`)
  carries no such hint, so filename keywords remain the fallback for uploads
  that have no `view` - the same heuristic the original branch used outright.

Two independent checks, both classic and offline (no model download, no API
key, easy to explain in a technical review):

  1. Blur check - Laplacian variance. A low variance means few high-frequency
     edges, i.e. the photo is soft or out of focus. Sharp clinical photos
     score high; motion-blurred or solid-colour images score near zero.
  2. Face check - OpenCV's bundled Haar cascade counts frontal faces. Enforced
     only for extraoral views (frontal face, smile, repose) - intraoral shots
     (occlusal, buccal, retracted) legitimately contain no face at all.

Both run on a downscaled, in-memory copy. The original bytes are never
decoded back to disk, re-encoded, or modified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Filename/view keywords used to decide whether a face is expected, when no
# explicit view hint is given.
_DEFAULT_FACE_REQUIRED_KEYWORDS = ("frontal", "smile", "repose", "face", "extraoral")
_DEFAULT_FACE_OPTIONAL_KEYWORDS = ("profile",)

# "Frontal" alone is genuinely ambiguous: the standard 5-view dental series
# includes a "frontal retracted" shot, which is *intraoral* - retractors hold
# the lips back and the camera shoots straight at the teeth, no face in frame
# at all. That is different from a "frontal repose" or "frontal smile", the
# extraoral portrait shots the keyword is meant to catch. An intraoral term
# anywhere in the name settles it regardless of "frontal" also being present.
_DEFAULT_INTRAORAL_OVERRIDE_KEYWORDS = (
    "retracted", "occlusal", "buccal", "lingual", "palatal", "intraoral",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityConfig:
    """Thresholds and rules for the quality gate. See backend/.env.example."""

    enabled: bool = True
    analysis_max_px: int = 512  # longest edge for the analysis copy
    blur_enabled: bool = True
    blur_min_variance: float = 60.0  # below this -> rejected as blurry
    face_enabled: bool = True
    face_required_keywords: tuple[str, ...] = _DEFAULT_FACE_REQUIRED_KEYWORDS
    face_optional_keywords: tuple[str, ...] = _DEFAULT_FACE_OPTIONAL_KEYWORDS
    intraoral_override_keywords: tuple[str, ...] = _DEFAULT_INTRAORAL_OVERRIDE_KEYWORDS
    min_face_px: int = 20  # minSize for the Haar detector


@dataclass
class QualityResult:
    """Outcome of running the quality gate on one image."""

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


def classify_view(filename: str, cfg: QualityConfig, view: str | None = None) -> str:
    """Decide whether a face is expected for this image.

    Returns "face_required" (a face must be present), "face_optional" (looked
    for, never rejected on its absence), or "no_face" (not applicable).
    `view` - the bridge's own label for the shot, when it sent one - is
    checked first; the filename is the fallback for anything that has none.
    An intraoral term (`retracted`, `occlusal`, ...) always wins over a bare
    "frontal", which is otherwise ambiguous between an intraoral and an
    extraoral shot.
    """
    haystack = f"{view or ''} {filename}".upper()
    if any(kw.upper() in haystack for kw in cfg.intraoral_override_keywords):
        return "no_face"
    if any(kw.upper() in haystack for kw in cfg.face_required_keywords):
        return "face_required"
    if any(kw.upper() in haystack for kw in cfg.face_optional_keywords):
        return "face_optional"
    return "no_face"


# ---------------------------------------------------------------------------
# OpenCV primitives
# ---------------------------------------------------------------------------


def _to_gray_array(image: Image.Image, max_px: int) -> np.ndarray:
    """Resize to <= max_px on the longest edge and return a grayscale array."""
    working = image
    if max(working.size) > max_px:
        working = working.copy()
        working.thumbnail((max_px, max_px), resample=Image.LANCZOS)
    return np.asarray(working.convert("L"))


def measure_blur(gray: np.ndarray) -> float:
    """Blur score = variance of the image Laplacian.

    A sharp image has many strong edges -> high variance. A blurry or flat one
    has few -> near zero. Always measured at the same analysis size, so the
    threshold stays resolution-independent.
    """
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def detect_faces(gray: np.ndarray, min_face_px: int = 20) -> int:
    """Count frontal faces using OpenCV's bundled Haar cascade.

    Heuristic, not learned on patient data, and runs entirely offline - the
    cascade ships inside opencv-python-headless, nothing is downloaded.
    """
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    if cascade.empty():
        log.warning("face cascade failed to load - face check disabled for this image")
        return 0
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_face_px, min_face_px)
    )
    return len(faces)


# ---------------------------------------------------------------------------
# Full gate
# ---------------------------------------------------------------------------


def assess_image(
    data: bytes,
    filename: str,
    cfg: QualityConfig,
    view: str | None = None,
) -> QualityResult:
    """Run the blur and (if applicable) face checks on a decoded capture.

    Takes the bytes already read for this request - never re-reads from disk,
    and the capture itself is never touched either way.
    """
    reasons: list[str] = []

    try:
        import io

        with Image.open(io.BytesIO(data)) as image:
            gray = _to_gray_array(image, cfg.analysis_max_px)
    except Exception as exc:
        # A RAW file Pillow cannot open, or genuinely corrupt bytes. Rejecting
        # here would be wrong - `chart_capture` already has its own tolerant
        # handling for undecodable images (stored, no thumbnail) - so the gate
        # simply has nothing to say about this file rather than blocking it.
        log.debug("quality gate could not decode %s: %s", filename, exc)
        return QualityResult(passed=True)

    blur_score = 0.0
    blur_rejected = False
    if cfg.blur_enabled:
        blur_score = measure_blur(gray)
        if blur_score < cfg.blur_min_variance:
            blur_rejected = True
            reasons.append(
                f"photo is blurry (sharpness {round(blur_score, 1)} "
                f"below {cfg.blur_min_variance})"
            )

    faces_detected = 0
    face_required = False
    face_rejected = False
    if cfg.face_enabled:
        view_kind = classify_view(filename, cfg, view)
        if view_kind in ("face_required", "face_optional"):
            face_required = view_kind == "face_required"
            faces_detected = detect_faces(gray, cfg.min_face_px)
            if view_kind == "face_required" and faces_detected == 0:
                face_rejected = True
                reasons.append("no face detected in frame - please retake")

    return QualityResult(
        passed=not reasons,
        blur_score=round(blur_score, 1),
        blur_rejected=blur_rejected,
        faces_detected=faces_detected,
        face_required=face_required,
        face_rejected=face_rejected,
        reasons=reasons,
    )
