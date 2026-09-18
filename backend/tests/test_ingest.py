"""
test_ingest.py — Unit tests for the core ingest pipeline.

All tests run without a physical camera, without a CareStack account,
and without any files outside of pytest's temporary directory.

Key test: test_original_file_unchanged_after_copy() proves the
"zero quality loss" guarantee by comparing SHA-256 hashes before and after
the full pipeline runs.
"""

import sys
from pathlib import Path

import pytest

# Allow imports from backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest import (
    compute_sha256,
    extract_capture_date,
    is_duplicate,
    load_manifest,
    process_file,
    resolve_patient,
    save_manifest,
)


# ── SHA-256 hashing ────────────────────────────────────────────────────────

class TestComputeSha256:
    def test_returns_64_char_hex_string(self, sample_jpeg):
        result = compute_sha256(sample_jpeg)
        assert isinstance(result, str)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_file_same_hash(self, sample_jpeg):
        """Hashing is deterministic — same file always returns same digest."""
        h1 = compute_sha256(sample_jpeg)
        h2 = compute_sha256(sample_jpeg)
        assert h1 == h2

    def test_different_files_different_hashes(self, sample_jpeg, sample_jpeg_no_exif):
        """Two different images must produce different hashes."""
        h1 = compute_sha256(sample_jpeg)
        h2 = compute_sha256(sample_jpeg_no_exif)
        assert h1 != h2


# ── Manifest / duplicate detection ────────────────────────────────────────

class TestManifest:
    def test_load_returns_empty_dict_when_no_file(self, tmp_dirs):
        manifest = load_manifest(tmp_dirs["manifest"])
        assert manifest == {}

    def test_save_and_reload(self, tmp_dirs):
        data = {"abc123": {"filename": "test.jpg"}}
        save_manifest(tmp_dirs["manifest"], data)
        loaded = load_manifest(tmp_dirs["manifest"])
        assert loaded == data

    def test_is_duplicate_true_when_hash_in_manifest(self):
        manifest = {"deadbeef": {"filename": "img.jpg"}}
        assert is_duplicate("deadbeef", manifest) is True

    def test_is_duplicate_false_when_hash_absent(self):
        manifest = {"other_hash": {"filename": "img.jpg"}}
        assert is_duplicate("deadbeef", manifest) is False

    def test_is_duplicate_false_on_empty_manifest(self):
        assert is_duplicate("anything", {}) is False


# ── EXIF extraction ────────────────────────────────────────────────────────

class TestExtractCaptureDate:
    def test_returns_date_string_format(self, sample_jpeg):
        date_str = extract_capture_date(sample_jpeg)
        # Must be YYYY-MM-DD
        parts = date_str.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4  # year
        assert len(parts[1]) == 2  # month
        assert len(parts[2]) == 2  # day

    def test_falls_back_to_mtime_when_no_exif(self, sample_jpeg_no_exif):
        """No EXIF → use mtime. Should still return a valid YYYY-MM-DD string."""
        date_str = extract_capture_date(sample_jpeg_no_exif)
        assert len(date_str) == 10
        assert date_str[4] == "-"
        assert date_str[7] == "-"


# ── Patient / session resolution ───────────────────────────────────────────

class TestResolvePatient:
    def test_active_session_matched_by_prefix(self, sample_config):
        """JOHN_IMG_4823.jpg → P001 (active, prefix=JOHN_)."""
        session = resolve_patient("JOHN_IMG_4823.jpg", sample_config)
        assert session.patient_id == "P001"
        assert session.patient_name == "Smith, John"

    def test_inactive_session_not_matched(self, sample_config):
        """JANE_IMG_4824.jpg — JANE_ session is inactive → falls to UNMATCHED."""
        session = resolve_patient("JANE_IMG_4824.jpg", sample_config)
        assert session.patient_id == "UNMATCHED"

    def test_unrecognised_filename_goes_to_default(self, sample_config):
        """IMG_4825.jpg has no matching prefix → UNMATCHED bucket."""
        session = resolve_patient("IMG_4825.jpg", sample_config)
        assert session.patient_id == "UNMATCHED"
        assert session.patient_name == "Unmatched Patient"

    def test_prefix_match_is_case_insensitive(self, sample_config):
        """Prefix matching must be case-insensitive (camera filenames can vary)."""
        session = resolve_patient("john_IMG_9999.jpg", sample_config)
        assert session.patient_id == "P001"


# ── Full pipeline ──────────────────────────────────────────────────────────

class TestProcessFile:
    def test_successful_ingest(self, sample_jpeg, sample_config, tmp_dirs):
        manifest = {}
        result = process_file(sample_jpeg, sample_config, manifest, tmp_dirs["outbox"])
        assert result.status == "ingested"
        assert result.patient_id == "P001"
        assert result.sha256 != ""
        assert result.original_dest != ""

    def test_original_file_unchanged_after_copy(self, sample_jpeg, sample_config, tmp_dirs):
        """
        ★ ZERO QUALITY LOSS PROOF ★

        The SHA-256 of the original source file must be identical before and
        after the pipeline runs. This guarantees that copy_original() uses
        byte-for-bit file copy and never decodes / re-encodes the image.
        """
        hash_before = compute_sha256(sample_jpeg)
        manifest = {}
        result = process_file(sample_jpeg, sample_config, manifest, tmp_dirs["outbox"])

        assert result.status == "ingested"
        # Verify the source file (in inbox) is untouched
        hash_after_source = compute_sha256(sample_jpeg)
        assert hash_before == hash_after_source, (
            "Source file was modified by the pipeline — quality loss possible!"
        )
        # Verify the copied file in outbox is bit-for-bit identical to the source
        hash_copy = compute_sha256(Path(result.original_dest))
        assert hash_before == hash_copy, (
            "Outbox copy does not match original — data was lost during copy!"
        )

    def test_duplicate_is_skipped(self, sample_jpeg, sample_config, tmp_dirs):
        """Second ingest of the same file returns status=duplicate and writes nothing new."""
        manifest = {}

        # First ingest
        result1 = process_file(sample_jpeg, sample_config, manifest, tmp_dirs["outbox"])
        assert result1.status == "ingested"

        # Count files in outbox before second ingest
        files_before = list(tmp_dirs["outbox"].rglob("*.*"))

        # Second ingest (same file, same hash)
        result2 = process_file(sample_jpeg, sample_config, manifest, tmp_dirs["outbox"])
        assert result2.status == "duplicate"

        # No new files written
        files_after = list(tmp_dirs["outbox"].rglob("*.*"))
        assert len(files_before) == len(files_after)

    def test_unsupported_extension_is_skipped(self, tmp_dirs, sample_config):
        """A .txt file must be skipped — not ingested, not errored."""
        txt_file = tmp_dirs["inbox"] / "notes.txt"
        txt_file.write_text("not an image")
        result = process_file(txt_file, sample_config, {}, tmp_dirs["outbox"])
        assert result.status == "skipped"

    def test_manifest_updated_after_ingest(self, sample_jpeg, sample_config, tmp_dirs):
        """The manifest dict must contain the new SHA-256 after a successful ingest."""
        manifest = {}
        result = process_file(sample_jpeg, sample_config, manifest, tmp_dirs["outbox"])
        assert result.sha256 in manifest
        assert manifest[result.sha256]["patient_id"] == "P001"

    def test_unmatched_file_goes_to_default_patient(
        self, sample_jpeg_unmatched, sample_config, tmp_dirs
    ):
        """IMG_4825.jpg has no prefix match → stored under UNMATCHED."""
        manifest = {}
        result = process_file(sample_jpeg_unmatched, sample_config, manifest, tmp_dirs["outbox"])
        assert result.status == "ingested"
        assert result.patient_id == "UNMATCHED"
        assert "UNMATCHED" in result.original_dest
