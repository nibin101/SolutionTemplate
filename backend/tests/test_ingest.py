"""The rules that keep an automated pipeline clinically safe."""

from __future__ import annotations

import hashlib
import io

from PIL import Image


def post_image(client, auth, payload: bytes, **form):
    return client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("IMG_0001.JPG", payload, "image/jpeg")},
        data={"source": "test", "operatory": "OP-1", **form},
    )


def open_session(client, patient_id: str, operatory: str = "OP-1"):
    return client.post(
        "/api/sessions", json={"patient_id": patient_id, "operatory": operatory}
    ).json()


def test_ingest_requires_a_token(client, jpeg):
    response = client.post(
        "/api/ingest",
        files={"file": ("IMG_0001.JPG", jpeg(), "image/jpeg")},
        data={"source": "test"},
    )
    assert response.status_code == 401


def test_capture_without_a_session_is_quarantined(client, auth, jpeg):
    body = post_image(client, auth, jpeg()).json()
    assert body["status"] == "stored"
    assert body["image"]["status"] == "unassigned"
    assert body["image"]["patient_id"] is None

    waiting = client.get("/api/images/unassigned").json()
    assert [image["id"] for image in waiting] == [body["image"]["id"]]


def test_capture_during_a_session_is_charted(client, auth, jpeg, patient):
    open_session(client, patient["id"])

    image = post_image(client, auth, jpeg()).json()["image"]
    assert image["status"] == "assigned"
    assert image["patient_id"] == patient["id"]
    assert len(client.get(f"/api/patients/{patient['id']}/images").json()) == 1


def test_a_session_only_claims_its_own_operatory(client, auth, jpeg, patient):
    open_session(client, patient["id"], operatory="OP-1")

    image = post_image(client, auth, jpeg(), operatory="OP-2").json()["image"]
    assert image["status"] == "unassigned", "a capture in another room must not be charted here"


def test_resending_the_same_bytes_is_a_no_op(client, auth, jpeg, patient):
    """A retry after a half-failed upload must not double-chart the photo."""
    open_session(client, patient["id"])
    payload = jpeg()

    first = post_image(client, auth, payload).json()
    second = post_image(client, auth, payload).json()

    assert first["status"] == "stored"
    assert second["status"] == "duplicate"
    assert second["image"]["id"] == first["image"]["id"]
    assert len(client.get(f"/api/patients/{patient['id']}/images").json()) == 1


def test_corrupted_transfer_is_refused(client, auth, jpeg):
    response = post_image(
        client, auth, jpeg(), content_hash=hashlib.sha256(b"different").hexdigest()
    )
    assert response.json() == {"status": "rejected", "reason": "hash mismatch"}


def test_opening_a_session_closes_the_previous_one(client, auth, jpeg, patient):
    other = client.post(
        "/api/patients",
        json={"chart_number": "T-2", "first_name": "Second", "last_name": "Patient"},
    ).json()

    first = open_session(client, patient["id"])
    open_session(client, other["id"])

    sessions = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[first["id"]]["status"] == "closed"

    image = post_image(client, auth, jpeg()).json()["image"]
    assert image["patient_id"] == other["id"]


def test_quarantined_image_can_be_assigned(client, auth, jpeg, patient):
    image = post_image(client, auth, jpeg()).json()["image"]

    assigned = client.post(
        f"/api/images/{image['id']}/assign", json={"patient_id": patient["id"]}
    ).json()

    assert assigned["status"] == "assigned"
    assert assigned["patient_id"] == patient["id"]
    assert client.get("/api/images/unassigned").json() == []


def test_exif_capture_time_and_camera_are_read(client, auth):
    """Camera metadata records *when the photo was taken*, not when it arrived."""
    exif = Image.Exif()
    exif[0x010F] = "Canon"
    exif[0x0110] = "EOS R6"
    exif.get_ifd(0x8769)[0x9003] = "2026:09:18 10:30:00"

    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), (10, 90, 160)).save(buffer, format="JPEG", exif=exif)

    image = post_image(client, auth, buffer.getvalue()).json()["image"]
    assert image["camera_make"] == "Canon"
    assert image["camera_model"] == "EOS R6"

    # The camera writes local clock time with no zone; the backend normalises it
    # to UTC, so compare against that same conversion rather than a fixed string.
    from datetime import datetime, timezone

    expected = (
        datetime(2026, 9, 18, 10, 30)
        .astimezone()
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
    assert image["captured_at"] == expected
