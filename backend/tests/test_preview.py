"""The mid-size preview tier.

Opening a photograph in the chair-side viewer must not pull a 24 MP original
across the practice network. These tests pin the three behaviours that
guarantees: a large capture gets a preview, a small one deliberately does not,
and the endpoint always returns *something* renderable either way.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.storage import PREVIEW_MAX_EDGE


def _jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (140, 90, 95)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _ingest(client, auth, data: bytes, name: str) -> dict:
    response = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": (name, data, "image/jpeg")},
        data={"source": "test", "operatory": "OP-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stored", body
    return body["image"]


def test_large_capture_gets_a_preview(client, auth):
    image = _ingest(client, auth, _jpeg(4032, 3024), "big.jpg")
    assert image["preview_url"] is not None


def test_preview_is_capped_to_the_long_edge(client, auth):
    image = _ingest(client, auth, _jpeg(4032, 3024), "big2.jpg")
    served = client.get(image["preview_url"])
    assert served.status_code == 200
    with Image.open(io.BytesIO(served.content)) as rendered:
        assert max(rendered.size) == PREVIEW_MAX_EDGE
        # Aspect ratio must survive, or the clinician is judging a stretched photo.
        assert rendered.size == (PREVIEW_MAX_EDGE, int(PREVIEW_MAX_EDGE * 3024 / 4032))


def test_preview_is_much_smaller_than_the_original(client, auth):
    original = _jpeg(4032, 3024)
    image = _ingest(client, auth, original, "big3.jpg")
    served = client.get(image["preview_url"])
    assert len(served.content) < len(original) / 2


def test_small_capture_gets_no_preview(client, auth):
    """Below the cap the original *is* the preview; a copy would earn nothing."""
    image = _ingest(client, auth, _jpeg(800, 600), "small.jpg")
    assert image["preview_url"] is None


def test_preview_endpoint_falls_back_to_the_original(client, auth):
    """A caller never has to special-case a missing preview."""
    image = _ingest(client, auth, _jpeg(800, 600), "small2.jpg")
    served = client.get(f"/api/images/{image['id']}/preview")
    assert served.status_code == 200
    with Image.open(io.BytesIO(served.content)) as rendered:
        assert rendered.size == (800, 600)


def test_original_is_never_modified(client, auth):
    """The whole point: deriving a preview must not touch the capture."""
    original = _jpeg(4032, 3024)
    image = _ingest(client, auth, original, "untouched.jpg")
    served = client.get(image["file_url"])
    assert served.content == original


def test_undecodable_capture_still_charts_without_a_preview(client, auth):
    """A RAW body file cannot be decoded by Pillow - it must still be stored."""
    image = _ingest(client, auth, b"II*\x00" + b"\x91" * 4096, "IMG_1.cr2")
    assert image["preview_url"] is None
    assert image["thumb_url"] is None
    # Still retrievable byte-for-byte, which is what actually matters.
    assert client.get(image["file_url"]).status_code == 200
