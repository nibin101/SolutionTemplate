"""
test_carestack.py — Unit tests for the CareStack client layer.

All tests use MockCareStackClient — no HTTP calls, no credentials needed.
The real CareStackClient is tested only for its interface contract (same
method signatures as the mock).
"""

import sys
from pathlib import Path
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from carestack import (
    BaseCareStackClient,
    CareStackClient,
    MockCareStackClient,
    UploadResult,
    make_carestack_client,
)


# ── MockCareStackClient ────────────────────────────────────────────────────

class TestMockCareStackClient:
    @pytest.mark.asyncio
    async def test_upload_returns_success(self, sample_jpeg):
        client = MockCareStackClient()
        result = await client.upload_image(
            patient_id="P001",
            file_path=sample_jpeg,
            document_name="IMG_4823.jpg",
            visit_date=date(2026, 9, 17),
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_upload_result_is_marked_mock(self, sample_jpeg):
        client = MockCareStackClient()
        result = await client.upload_image(
            patient_id="P001",
            file_path=sample_jpeg,
            document_name="test.jpg",
            visit_date=date.today(),
        )
        assert result.mock is True

    @pytest.mark.asyncio
    async def test_upload_document_id_contains_patient_id(self, sample_jpeg):
        """Mock doc ID should reference the patient for traceability."""
        client = MockCareStackClient()
        result = await client.upload_image(
            patient_id="P001",
            file_path=sample_jpeg,
            document_name="test.jpg",
            visit_date=date.today(),
        )
        assert "P001" in result.document_id

    @pytest.mark.asyncio
    async def test_upload_with_tags(self, sample_jpeg):
        """Tags should be accepted without error."""
        client = MockCareStackClient()
        result = await client.upload_image(
            patient_id="P001",
            file_path=sample_jpeg,
            document_name="test.jpg",
            visit_date=date.today(),
            tags=["anterior", "occlusal"],
        )
        assert result.success is True


# ── make_carestack_client factory ──────────────────────────────────────────

class TestMakeCareStackClient:
    def test_mock_true_returns_mock_client(self):
        client = make_carestack_client(mock=True)
        assert isinstance(client, MockCareStackClient)

    def test_mock_false_with_valid_args_returns_real_client(self):
        client = make_carestack_client(
            mock=False,
            base_url="https://api.example.com",
            client_id="id",
            client_secret="secret",
        )
        assert isinstance(client, CareStackClient)

    def test_mock_false_missing_credentials_raises(self):
        with pytest.raises(ValueError, match="CARESTACK_MOCK"):
            make_carestack_client(mock=False, base_url="", client_id="", client_secret="")


# ── Interface contract ─────────────────────────────────────────────────────

class TestInterfaceContract:
    def test_mock_client_is_subclass_of_base(self):
        """Both clients must implement BaseCareStackClient."""
        assert issubclass(MockCareStackClient, BaseCareStackClient)
        assert issubclass(CareStackClient, BaseCareStackClient)

    def test_upload_result_fields(self):
        r = UploadResult(success=True, document_id="doc-1", status_code=201, mock=True)
        assert r.success is True
        assert r.document_id == "doc-1"
        assert r.mock is True
