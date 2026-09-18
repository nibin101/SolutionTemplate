"""
carestack.py — CareStack API client and mock for the dental camera workflow.

The real client (CareStackClient) sends a multipart POST to the CareStack
Patient Documents API:
  POST {base_url}/api/v1.0/patients/{patient_id}/documents

The mock client (MockCareStackClient) logs the call and returns a successful
result without making any HTTP request. Both classes share the same interface
(upload_image) so switching from mock to real is a one-line change in main.py.

CareStack Developer Portal: https://developer.carestack.com
API resource: Patient Documents (Resource 06)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class UploadResult:
    success: bool
    document_id: str = ""   # CareStack document ID returned on success
    status_code: int = 0
    error_message: str = ""
    mock: bool = False       # True if this result came from MockCareStackClient


# ---------------------------------------------------------------------------
# Abstract base (shared interface)
# ---------------------------------------------------------------------------

class BaseCareStackClient(ABC):
    @abstractmethod
    async def upload_image(
        self,
        patient_id: str,
        file_path: Path,
        document_name: str,
        visit_date: date,
        tags: list[str] | None = None,
    ) -> UploadResult:
        """Upload an image file to the patient's CareStack document record."""


# ---------------------------------------------------------------------------
# Real HTTP client
# ---------------------------------------------------------------------------

class CareStackClient(BaseCareStackClient):
    """
    Calls the CareStack REST API to upload a patient document (image).

    Authentication uses OAuth2 client credentials (client_id + client_secret)
    to obtain a Bearer token, then attaches it to the upload request.
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None

    async def _get_token(self) -> str:
        """Obtain an OAuth2 Bearer token from CareStack."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=10.0,
            )
            response.raise_for_status()
            return response.json()["access_token"]

    async def upload_image(
        self,
        patient_id: str,
        file_path: Path,
        document_name: str,
        visit_date: date,
        tags: list[str] | None = None,
    ) -> UploadResult:
        import httpx

        if not self._token:
            self._token = await self._get_token()

        url = f"{self._base_url}/api/v1.0/patients/{patient_id}/documents"
        headers = {"Authorization": f"Bearer {self._token}"}

        try:
            async with httpx.AsyncClient() as client:
                with open(file_path, "rb") as fh:
                    response = await client.post(
                        url,
                        headers=headers,
                        files={"file": (file_path.name, fh, _mime_type(file_path))},
                        data={
                            "DocumentType": "ClinicalPhoto",
                            "Description": document_name,
                            "VisitDate": visit_date.isoformat(),
                            "Tags": ",".join(tags or []),
                        },
                        timeout=30.0,
                    )

            if response.status_code in (200, 201):
                doc_id = response.json().get("id", "")
                logger.info("CareStack upload OK: patient=%s doc=%s", patient_id, doc_id)
                return UploadResult(success=True, document_id=doc_id, status_code=response.status_code)
            else:
                logger.error(
                    "CareStack upload failed: patient=%s status=%d body=%s",
                    patient_id, response.status_code, response.text[:200],
                )
                return UploadResult(
                    success=False,
                    status_code=response.status_code,
                    error_message=response.text[:200],
                )

        except Exception as exc:
            logger.exception("CareStack upload exception for patient %s", patient_id)
            return UploadResult(success=False, error_message=str(exc))


# ---------------------------------------------------------------------------
# Mock client (demo / development — no HTTP, no credentials needed)
# ---------------------------------------------------------------------------

class MockCareStackClient(BaseCareStackClient):
    """
    Drop-in replacement for CareStackClient that logs calls without HTTP.

    Used when CARESTACK_MOCK=true in .env. Allows the full pipeline — including
    the CareStack upload step — to be demonstrated and tested without real
    credentials or a live CareStack instance.
    """

    async def upload_image(
        self,
        patient_id: str,
        file_path: Path,
        document_name: str,
        visit_date: date,
        tags: list[str] | None = None,
    ) -> UploadResult:
        logger.info(
            "[MOCK] CareStack upload — patient=%s file=%s name=%s date=%s tags=%s",
            patient_id,
            file_path.name,
            document_name,
            visit_date.isoformat(),
            tags,
        )
        # Simulate a successful upload with a fake document ID
        fake_doc_id = f"mock-doc-{patient_id}-{file_path.stem}"
        return UploadResult(
            success=True,
            document_id=fake_doc_id,
            status_code=201,
            mock=True,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_carestack_client(
    mock: bool,
    base_url: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> BaseCareStackClient:
    """
    Return the appropriate CareStack client based on the CARESTACK_MOCK setting.

    This factory is the single place in the codebase where the choice between
    real and mock is made — all other code depends only on BaseCareStackClient.
    """
    if mock:
        logger.info("CareStack client: MOCK mode (no real HTTP calls)")
        return MockCareStackClient()

    if not all([base_url, client_id, client_secret]):
        raise ValueError(
            "CARESTACK_MOCK is false but CARESTACK_BASE_URL / CLIENT_ID / "
            "CLIENT_SECRET are not all set in .env"
        )
    logger.info("CareStack client: REAL mode → %s", base_url)
    return CareStackClient(base_url, client_id, client_secret)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mime_type(file_path: Path) -> str:
    """Return the MIME type for the file based on its extension."""
    ext = file_path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".cr2": "image/x-canon-cr2",
        ".cr3": "image/x-canon-cr3",
        ".nef": "image/x-nikon-nef",
        ".arw": "image/x-sony-arw",
        ".orf": "image/x-olympus-orf",
        ".rw2": "image/x-panasonic-rw2",
    }.get(ext, "application/octet-stream")
