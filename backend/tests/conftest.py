"""Test fixtures.

The backend reads its settings once at import time, so the environment is
pointed at a throwaway data directory *before* `app` is imported. Each test then
starts from an empty database rather than a fresh directory, which keeps the
settings object immutable the way production has it.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="snapchart-test-"))
os.environ.setdefault("BRIDGE_TOKEN", "test-token")
os.environ.setdefault("SERVE_FRONTEND", "false")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.db import init_db, session as db_session  # noqa: E402
from app.main import app  # noqa: E402

# Child tables first: foreign keys are enforced, so patients cannot be cleared
# until everything referencing them has gone.
TABLES = (
    "images",
    "capture_sessions",
    "patients",
    "bridge_status",
    "audit_log",
)


@pytest.fixture()
def jpeg():
    """Factory for a real (tiny) JPEG - the ingest path decodes what it is given."""

    def make(colour: tuple[int, int, int] = (200, 120, 120)) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (64, 48), colour).save(buffer, format="JPEG")
        return buffer.getvalue()

    return make


@pytest.fixture()
def client():
    init_db()
    with db_session() as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth() -> dict[str, str]:
    return {"X-Bridge-Token": "test-token"}


@pytest.fixture()
def patient(client):
    response = client.post(
        "/api/patients",
        json={"chart_number": "T-1", "first_name": "Test", "last_name": "Patient"},
    )
    assert response.status_code == 201
    return response.json()
