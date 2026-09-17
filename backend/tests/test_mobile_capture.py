"""Phone-as-camera: photographs pushed from a handset over Wi-Fi.

The point of these tests is that a phone gets no special treatment - it routes
by operatory, quarantines when nobody is in the chair, and deduplicates exactly
as the USB bridge does.
"""

from __future__ import annotations


def send(client, jpeg_bytes, **form):
    return client.post(
        "/api/capture",
        files=[("files", ("phone.jpg", jpeg_bytes, "image/jpeg"))],
        data={"operatory": "OP-1", **form},
    )


def test_phone_photo_is_charted_to_the_open_session(client, jpeg, patient):
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})

    body = send(client, jpeg()).json()
    assert body["stored"] == 1

    image = body["images"][0]
    assert image["status"] == "assigned"
    assert image["patient_id"] == patient["id"]
    assert image["source"] == "mobile"


def test_phone_photo_with_no_session_is_held_for_review(client, jpeg):
    body = send(client, jpeg()).json()
    assert body["images"][0]["status"] == "unassigned"
    assert len(client.get("/api/images/unassigned").json()) == 1


def test_phone_photo_follows_the_room_it_says_it_is_in(client, jpeg, patient):
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})

    body = send(client, jpeg(), operatory="OP-2").json()
    assert body["images"][0]["status"] == "unassigned", "another room must not be charted here"


def test_a_named_patient_overrides_the_room(client, jpeg, patient):
    other = client.post(
        "/api/patients",
        json={"chart_number": "T-9", "first_name": "Override", "last_name": "Target"},
    ).json()
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})

    body = send(client, jpeg(), patient_id=other["id"]).json()
    assert body["images"][0]["patient_id"] == other["id"]


def test_the_device_name_is_recorded(client, jpeg, patient):
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})
    body = send(client, jpeg(), device="iPhone").json()
    assert body["images"][0]["camera_model"] == "iPhone"


def test_resending_the_same_photo_is_a_no_op(client, jpeg, patient):
    """A phone on a flaky Wi-Fi will retry; that must not double-chart."""
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})
    payload = jpeg()

    assert send(client, payload).json()["stored"] == 1
    second = send(client, payload).json()
    assert second["stored"] == 0 and second["duplicates"] == 1
    assert len(client.get(f"/api/patients/{patient['id']}/images").json()) == 1


def test_phone_photo_lands_in_the_patient_folder(client, jpeg, patient):
    from pathlib import Path

    from app.db import session as db_session

    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})
    image = send(client, jpeg()).json()["images"][0]

    with db_session() as conn:
        row = conn.execute(
            "SELECT stored_path FROM images WHERE id=?", (image["id"],)
        ).fetchone()

    path = Path(row["stored_path"])
    assert path.exists()
    assert path.parent.parent.name == "Test_Patient_T-1"


def test_unknown_patient_is_refused(client, jpeg):
    assert send(client, jpeg(), patient_id="nope").status_code == 404


def test_oversized_batch_is_refused(client, jpeg):
    files = [("files", (f"{i}.jpg", jpeg((i, i, i)), "image/jpeg")) for i in range(31)]
    response = client.post("/api/capture", files=files, data={"operatory": "OP-1"})
    assert response.status_code == 400
