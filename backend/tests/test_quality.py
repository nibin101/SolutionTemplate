"""
test_quality.py — Unit tests for the CV quality gate (blur + face detection).

Covers:
  - View classification (which photos require a face)
  - Blur scoring via Laplacian variance
  - The assess_image gate (pass / reject with reasons)
  - process_file integration: rejected images are NOT stored in the outbox
    and are NOT recorded in the manifest / not uploaded.

No camera, no CareStack credentials, no network required. The positive
face-detection test uses a committed demo face photo and is auto-skipped if
that asset is missing.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest import process_file
from preview import decode_source
from quality import QualityConfig, assess_image, classify_view, detect_faces, measure_blur


def _gray(source) -> np.ndarray:
    """Decode a fixture image and return a grayscale numpy array."""
    return np.asarray(decode_source(source).convert("L"))


# ── View classification ────────────────────────────────────────────────────

class TestClassifyView:
    def test_extraoral_keywords_require_face(self, quality_config):
        for name in (
            "JOHN_01_frontal_repose.jpg",
            "JOHN_02_full_smile.jpg",
            "JOHN_03_face.jpg",
            "JOHN_04_extraoral.jpg",
        ):
            assert classify_view(name, quality_config) == "face_required", name

    def test_profile_is_optional(self, quality_config):
        assert classify_view("JOHN_05_right_profile.jpg", quality_config) == "face_optional"

    def test_intraoral_views_do_not_require_face(self, quality_config):
        for name in (
            "JOHN_06_anterior_occlusion.jpg",
            "JOHN_07_right_buccal.jpg",
            "JOHN_08_maxillary_occlusal.jpg",
            "JOHN_09_mandibular_occlusal.jpg",
        ):
            assert classify_view(name, quality_config) == "no_face", name

    def test_unknown_filename_is_no_face(self, quality_config):
        assert classify_view("IMG_4823.jpg", quality_config) == "no_face"

    def test_keyword_matching_is_case_insensitive(self, quality_config):
        assert classify_view("john_01_FRONTAL_repose.jpg", quality_config) == "face_required"


# ── Blur scoring ───────────────────────────────────────────────────────────

class TestMeasureBlur:
    def test_sharp_scores_higher_than_blurred(self, sharp_jpeg, blurry_jpeg, quality_config):
        assert measure_blur(_gray(sharp_jpeg)) > measure_blur(_gray(blurry_jpeg)) * 10


# ── assess_image gate ──────────────────────────────────────────────────────

class TestAssessImage:
    def test_sharp_extraoral_with_face_passes(self, face_jpeg, quality_config):
        """A sharp photo of a real face, named as a frontal view → passes."""
        result = assess_image(face_jpeg, quality_config)
        assert result.passed is True
        assert result.faces_detected >= 1

    def test_blurry_extraoral_rejected_for_blur(self, blurry_jpeg, quality_config):
        result = assess_image(blurry_jpeg, quality_config)
        assert result.passed is False
        assert result.blur_rejected is True
        assert any("blur" in r.lower() for r in result.reasons)

    def test_sharp_extraoral_without_face_rejected(self, sharp_jpeg, quality_config):
        """Frontal-view filename but no face in the frame → rejected."""
        result = assess_image(sharp_jpeg, quality_config)

        assert result.passed is False
        assert result.face_required is True
        assert result.faces_detected == 0
        assert result.face_rejected is True

    def test_sharp_intraoral_without_face_passes(self, occlusal_jpeg, quality_config):
        """Intraoral shot with no face must still pass — face not required."""
        result = assess_image(occlusal_jpeg, quality_config)
        assert result.passed is True
        assert result.face_required is False

    def test_corrupt_file_rejected(self, tmp_dirs, quality_config):
        bad = tmp_dirs["inbox"] / "JOHN_corrupt_frontal.jpg"
        bad.write_bytes(b"this is definitely not an image")
        result = assess_image(bad, quality_config)
        assert result.passed is False
        assert any("analyse" in r.lower() for r in result.reasons)


# ── Face detector primitive ────────────────────────────────────────────────

class TestDetectFaces:
    def test_no_faces_in_checkerboard(self, sharp_jpeg, quality_config):
        assert detect_faces(_gray(sharp_jpeg)) == 0

    def test_face_detected_in_face_photo(self, face_jpeg):
        assert detect_faces(_gray(face_jpeg)) >= 1


# ── process_file integration ───────────────────────────────────────────────

class TestProcessFileQualityGate:
    def test_blurry_extraoral_file_is_rejected(self, blurry_jpeg, sample_config_quality, tmp_dirs):
        manifest = {}
        result = process_file(blurry_jpeg, sample_config_quality, manifest, tmp_dirs["outbox"])

        assert result.status == "rejected"
        assert result.quality_reasons, "expected at least one rejection reason"
        assert any("blur" in r.lower() for r in result.quality_reasons)

    def test_rejected_file_writes_nothing_to_outbox(
        self, blurry_jpeg, sample_config_quality, tmp_dirs
    ):
        manifest = {}
        process_file(blurry_jpeg, sample_config_quality, manifest, tmp_dirs["outbox"])

        stored = list(tmp_dirs["outbox"].rglob("*.*"))
        assert stored == [], f"rejected image must not be archived: {stored}"

    def test_rejected_file_not_added_to_manifest(
        self, blurry_jpeg, sample_config_quality, tmp_dirs
    ):
        manifest = {}
        result = process_file(blurry_jpeg, sample_config_quality, manifest, tmp_dirs["outbox"])
        assert result.status == "rejected"
        assert manifest == {}, "rejected image must not be recorded in the manifest"

    def test_sharp_intraoral_passes_gate_and_is_ingested(
        self, occlusal_jpeg, sample_config_quality, tmp_dirs
    ):
        manifest = {}
        result = process_file(occlusal_jpeg, sample_config_quality, manifest, tmp_dirs["outbox"])
        assert result.status == "ingested"
        assert result.carestack_status == "pending"
        assert result.original_dest != ""

    def test_quality_disabled_skips_gate(self, sharp_jpeg, sample_config, tmp_dirs):
        """With the gate disabled, even a face-less frontal image is ingested."""
        manifest = {}
        result = process_file(sharp_jpeg, sample_config, manifest, tmp_dirs["outbox"])
        assert result.status == "ingested"

    def test_valid_face_photo_ingested_with_quality_notes(
        self, face_jpeg, sample_config_quality, tmp_dirs
    ):
        manifest = {}
        result = process_file(face_jpeg, sample_config_quality, manifest, tmp_dirs["outbox"])
        assert result.status == "ingested"
        assert result.quality_faces >= 1