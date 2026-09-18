"""Deleting a capture.

A camera fires off lens caps, blurred frames and accidental shots. Getting rid
of one has to take the photograph off the chart *and* off the disk - a practice
that believes a photo is gone while the file is still sitting in a patient
folder has a records problem, not a tidiness problem.
"""

from __future__ import annotations

from pathlib import Path

from app.db import session as db_session


def post_image(client, auth, payload: bytes, **form) -> dict:
    response = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("IMG_0001.JPG", payload, "image/jpeg")},
        data={"source": "test", "operatory": "OP-1", **form},
    )
    body = response.json()
    assert body["status"] == "stored", body
    return body["image"]


def paths_of(image_id: str) -> tuple[Path, list[Path]]:
    """The stored file and its derivatives, read straight from the database."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT stored_path, thumb_path, preview_path FROM images WHERE id=?",
            (image_id,),
        ).fetchone()
    derived = [Path(row[column]) for column in ("thumb_path", "preview_path") if row[column]]
    return Path(row["stored_path"]), derived


def test_delete_removes_the_row_and_the_file(client, auth, jpeg):
    image = post_image(client, auth, jpeg())
    stored, derived = paths_of(image["id"])
    assert stored.exists()

    assert client.delete(f"/api/images/{image['id']}").status_code == 204

    assert not stored.exists()
    for path in derived:
        assert not path.exists(), f"{path.name} outlived the capture it belongs to"
    assert client.get(f"/api/images/{image['id']}/file").status_code == 404


def test_deleted_capture_leaves_every_view(client, auth, jpeg, patient):
    """Off the chart, out of recent, and out of the needs-assignment queue."""
    session = client.post(
        "/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"}
    ).json()
    charted = post_image(client, auth, jpeg((200, 120, 120)), session_id=session["id"])
    client.post(f"/api/sessions/{session['id']}/end")

    # A different operatory, which has never had a session open - OP-1 would
    # still chart this one, by design (see the capture window in app/capture.py).
    waiting = post_image(client, auth, jpeg((120, 200, 140)), operatory="OP-3")

    assert waiting["status"] == "unassigned"
    assert len(client.get("/api/images/unassigned").json()) == 1

    client.delete(f"/api/images/{charted['id']}")
    client.delete(f"/api/images/{waiting['id']}")

    assert client.get(f"/api/patients/{patient['id']}/images").json() == []
    assert client.get("/api/images/unassigned").json() == []
    assert client.get("/api/images/recent").json() == []
    assert client.get("/api/images/folders").json() == []


def test_delete_is_audited(client, auth, jpeg):
    image = post_image(client, auth, jpeg())
    client.delete(f"/api/images/{image['id']}")

    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action='image.delete' AND subject=?",
            (image["id"],),
        ).fetchone()
    assert row is not None, "removing patient data has to leave a trail"


def test_deleting_an_unknown_capture_is_a_404(client):
    assert client.delete("/api/images/does-not-exist").status_code == 404


def test_delete_survives_a_file_that_has_already_gone(client, auth, jpeg):
    """Someone cleaned up in Explorer first; the row must still go."""
    image = post_image(client, auth, jpeg())
    stored, _ = paths_of(image["id"])
    stored.unlink()

    assert client.delete(f"/api/images/{image['id']}").status_code == 204
    assert client.get("/api/images/recent").json() == []
