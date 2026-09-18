"""
preview.py — Safe JPEG preview generation for the dental camera workflow.

Key design guarantee:
  The original image file is NEVER modified. This module opens a COPY of the
  image (or decodes directly from the source into memory), resizes it, and
  saves the result to a SEPARATE file path (_preview.jpg). The source file
  on disk is not touched.

Supported input formats:
  - JPEG / PNG / TIFF (via Pillow)
  - RAW formats: CR2, CR3, NEF, ARW, ORF, RW2 (via rawpy → libraw)

rawpy requirement: system package 'libraw-dev' must be installed first:
  sudo apt install libraw-dev
"""

import logging
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# File extensions that require rawpy for decoding.
# Pillow cannot natively open these proprietary RAW formats.
_RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2"}


def _open_with_pillow(source: Path) -> Image.Image:
    """Open a JPEG/PNG/TIFF with Pillow and auto-rotate via EXIF orientation."""
    img = Image.open(source)
    img = ImageOps.exif_transpose(img)  # respect EXIF rotation without re-encoding
    # Convert to RGB to ensure JPEG compatibility (e.g. PNG with alpha)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def _open_with_rawpy(source: Path) -> Image.Image:
    """
    Decode a RAW file (CR2/CR3/NEF/ARW/ORF/RW2) using rawpy (libraw).

    rawpy reads the RAW sensor data and performs demosaicing. The result is a
    16-bit or 8-bit numpy array which we hand to Pillow. The original file is
    never modified — rawpy only reads it.
    """
    try:
        import rawpy  # type: ignore
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "rawpy is not installed or libraw-dev is missing. "
            "Run: sudo apt install libraw-dev && pip install rawpy"
        ) from exc

    with rawpy.imread(str(source)) as raw:
        # postprocess() returns an 8-bit RGB numpy array
        rgb_array = raw.postprocess(
            use_camera_wb=True,   # use the camera's white balance setting
            half_size=False,      # full resolution
            no_auto_bright=True,  # preserve exposure as shot
        )

    return Image.fromarray(rgb_array)


def decode_source(source: Path) -> Image.Image:
    """
    Decode an image file into a PIL Image object (auto-rotated, RGB).

    Supports JPEG/PNG/TIFF via Pillow and RAW formats (CR2/CR3/NEF/ARW/ORF/RW2)
    via rawpy. The source file is only ever *read* — never modified — which is
    the base guarantee for both preview generation and quality analysis.
    """
    suffix = source.suffix.lower()

    # Choose the right decoder based on extension
    if suffix in _RAW_EXTENSIONS:
        return _open_with_rawpy(source)
    return _open_with_pillow(source)


def generate_preview(
    source: Path,
    destination: Path,
    max_px: int = 2048,
    quality: int = 92,
) -> Path:
    """
    Generate a JPEG preview of an image, scaled so the longest edge ≤ max_px.

    Parameters
    ----------
    source      : original image (never modified)
    destination : path where the preview JPEG will be written
    max_px      : longest-edge pixel cap (default 2048 — good for web + CareStack)
    quality     : JPEG quality 0–95 (default 92 — visually lossless at preview size)

    Returns
    -------
    The destination Path on success.

    Raises
    ------
    RuntimeError if the file cannot be opened (unsupported format or corrupt file).
    """
    img = decode_source(source)

    # Resize — thumbnail() modifies in-place and respects aspect ratio
    img.thumbnail((max_px, max_px), resample=Image.LANCZOS)

    destination.parent.mkdir(parents=True, exist_ok=True)
    img.save(destination, format="JPEG", quality=quality, optimize=True)

    logger.info(
        "Preview generated: %s → %s (%dx%d, quality=%d)",
        source.name,
        destination.name,
        img.width,
        img.height,
        quality,
    )
    return destination
