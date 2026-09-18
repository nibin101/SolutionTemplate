"""Unit tests for the CareStack client layer.

Adapted from the parallel MVP on `main`. Two deliberate changes for this
branch: the client lives in `app.carestack` (master keeps backend code under
`app/`), and the coroutines are driven with `asyncio.run` rather than
`pytest.mark.asyncio`, so the suite gains no new dependency - master does not
install pytest-asyncio and does not need it for four awaits.

Every test uses MockCareStackClient: no HTTP, no credentials. The real
CareStackClient is checked only for its interface contract.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.carestack import (
    BaseCareStackClient,
    CareStackClient,
    MockCareStackClient,
    UploadResult,
    make_carestack_client,
)


@pytest.fixture()
def sample_jpeg(tmp_path, jpeg):
    """A real JPEG on disk - the client opens the path it is given."""
    path = tmp_path / "IMG_4823.jpg"
    path.write_bytes(jpeg())
    return path


# -- MockCareStackClient ---------------------------------------------------


class TestMockCareStackClient:
    def test_upload_returns_success(self, sample_jpeg):
        result = asyncio.run(
            MockCareStackClient().upload_image(
                patient_id="P001",
                file_path=sample_jpeg,
                document_name="IMG_4823.jpg",
                visit_date=date(2026, 9, 17),
            )
        )
        assert result.success is True

    def test_upload_result_is_marked_mock(self, sample_jpeg):
        result = asyncio.run(
            MockCareStackClient().upload_image(
                patient_id="P001",
                file_path=sample_jpeg,
                document_name="test.jpg",
                visit_date=date.today(),
            )
        )
        assert result.mock is True

    def test_upload_document_id_contains_patient_id(self, sample_jpeg):
        """Mock doc id references the patient, so a demo stays traceable."""
        result = asyncio.run(
            MockCareStackClient().upload_image(
                patient_id="P001",
                file_path=sample_jpeg,
                document_name="test.jpg",
                visit_date=date.today(),
            )
        )
        assert "P001" in result.document_id

    def test_upload_with_tags(self, sample_jpeg):
        result = asyncio.run(
            MockCareStackClient().upload_image(
                patient_id="P001",
                file_path=sample_jpeg,
                document_name="test.jpg",
                visit_date=date.today(),
                tags=["anterior", "occlusal"],
            )
        )
        assert result.success is True


# -- make_carestack_client -------------------------------------------------


class TestMakeCareStackClient:
    def test_mock_true_returns_mock_client(self):
        assert isinstance(make_carestack_client(mock=True), MockCareStackClient)

    def test_mock_false_with_valid_args_returns_real_client(self):
        client = make_carestack_client(
            mock=False,
            base_url="https://api.example.com",
            client_id="id",
            client_secret="secret",
        )
        assert isinstance(client, CareStackClient)

    def test_mock_false_missing_credentials_raises(self):
        """Refusing to start half-configured beats failing on the first upload."""
        with pytest.raises(ValueError, match="CARESTACK_MOCK"):
            make_carestack_client(mock=False, base_url="", client_id="", client_secret="")


# -- interface contract ----------------------------------------------------


class TestInterfaceContract:
    def test_both_clients_implement_the_base(self):
        assert issubclass(MockCareStackClient, BaseCareStackClient)
        assert issubclass(CareStackClient, BaseCareStackClient)

    def test_upload_result_fields(self):
        result = UploadResult(success=True, document_id="doc-1", status_code=201, mock=True)
        assert (result.success, result.document_id, result.mock) == (True, "doc-1", True)
