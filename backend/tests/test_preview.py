"""
test_preview.py — Unit tests for preview generation.

Verifies that:
  - The original image file is NEVER modified (zero quality loss guarantee)
  - The preview is a valid JPEG at or below the configured max dimensions
  - The preview is a separate file from the original
"""

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest import compute_sha256
from preview import generate_preview


class TestGeneratePreview:
    def test_original_file_unchanged_after_preview(self, sample_jpeg, tmp_dirs):
        """
        ★ ZERO QUALITY LOSS PROOF (preview step) ★

        The source file's SHA-256 must be identical before and after preview
        generation. This confirms generate_preview() reads the file but never
        writes back to it.
        """
        hash_before = compute_sha256(sample_jpeg)

        dest = tmp_dirs["outbox"] / "preview.jpg"
        generate_preview(sample_jpeg, dest, max_px=256, quality=80)

        hash_after = compute_sha256(sample_jpeg)
        assert hash_before == hash_after, (
            "generate_preview() modified the source file — quality loss possible!"
        )

    def test_preview_file_is_created(self, sample_jpeg, tmp_dirs):
        dest = tmp_dirs["outbox"] / "preview.jpg"
        result = generate_preview(sample_jpeg, dest, max_px=256, quality=80)
        assert result.exists()
        assert result.stat().st_size > 0

    def test_preview_dimensions_within_limit(self, sample_jpeg, tmp_dirs):
        """Longest edge of the preview must not exceed max_px."""
        max_px = 256
        dest = tmp_dirs["outbox"] / "preview.jpg"
        generate_preview(sample_jpeg, dest, max_px=max_px, quality=80)

        img = Image.open(dest)
        assert max(img.width, img.height) <= max_px

    def test_preview_is_jpeg(self, sample_jpeg, tmp_dirs):
        dest = tmp_dirs["outbox"] / "preview.jpg"
        generate_preview(sample_jpeg, dest, max_px=256, quality=80)
        img = Image.open(dest)
        assert img.format == "JPEG"

    def test_preview_different_from_original(self, sample_jpeg, tmp_dirs):
        """The preview file must not be the same file as the original."""
        dest = tmp_dirs["outbox"] / "preview.jpg"
        generate_preview(sample_jpeg, dest, max_px=256, quality=80)
        assert dest.resolve() != sample_jpeg.resolve()

    def test_preview_smaller_than_original(self, sample_jpeg, tmp_dirs):
        """
        The preview should be smaller in file size than the original
        (when the original is larger than max_px).
        Our sample_jpeg is 800×600, max_px=256 — so preview must be smaller.
        """
        dest = tmp_dirs["outbox"] / "preview.jpg"
        generate_preview(sample_jpeg, dest, max_px=256, quality=80)
        assert dest.stat().st_size < sample_jpeg.stat().st_size

    def test_creates_parent_dirs_if_missing(self, sample_jpeg, tmp_dirs):
        """generate_preview should create any missing parent directories."""
        dest = tmp_dirs["outbox"] / "nested" / "dir" / "preview.jpg"
        generate_preview(sample_jpeg, dest, max_px=256, quality=80)
        assert dest.exists()
