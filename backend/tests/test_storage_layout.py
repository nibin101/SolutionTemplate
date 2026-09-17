"""Where photographs land on disk, and manual upload.

The folder layout is a deliberate feature, not an implementation detail: if this
software is ever unavailable, the practice still needs an organised folder of
clinical photographs it can hand to a specialist.
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.storage import UNASSIGNED_FOLDER, patient_folder, safe_component


def post_image(client, auth, payload: bytes, **form):
    return client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("IMG_0001.JPG", payload, "image/jpeg")},
        data={"source": "test", "operatory": "OP-1", **form},
    )


def stored_path(client, image_id: str) -> Path:
    """Read the path straight from the database rather than trusting the API."""
    from app.db import session as db_session

    with db_session() as conn:
        row = conn.execute(
            "SELECT stored_path FROM images WHERE id=?", (image_id,)
        ).fetchone()
    return Path(row["stored_path"])


def test_folder_names_are_filesystem_safe():
    assert patient_folder("CS/10:01", "Aarav", "Menon") == "Aarav_Menon_CS1001"
    assert safe_component('bad<>:"/\\|?*name') == "badname"
    assert safe_component("   ") == "unnamed"


def test_charted_photo_lands_in_the_patient_folder(client, auth, jpeg, patient):
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})
    image = post_image(client, auth, jpeg()).json()["image"]

    path = stored_path(client, image["id"])
    assert path.exists()
    assert path.parent.parent.name == "Test_Patient_T-1"
    # One folder per day of capture.
    assert len(path.parent.name) == 10 and path.parent.name.count("-") == 2


def test_unassigned_photo_is_quarantined_on_disk_too(client, auth, jpeg):
    image = post_image(client, auth, jpeg()).json()["image"]
    path = stored_path(client, image["id"])
    assert path.parent.parent.name == UNASSIGNED_FOLDER


def test_assigning_moves_the_file_into_the_patient_folder(client, auth, jpeg, patient):
    image = post_image(client, auth, jpeg()).json()["image"]
    before = stored_path(client, image["id"])
    assert before.parent.parent.name == UNASSIGNED_FOLDER

    client.post(f"/api/images/{image['id']}/assign", json={"patient_id": patient["id"]})

    after = stored_path(client, image["id"])
    assert after.exists(), "the photograph must survive the move"
    assert not before.exists(), "it must not be left behind in quarantine"
    assert after.parent.parent.name == "Test_Patient_T-1"


def test_two_photos_with_the_same_name_do_not_overwrite(client, auth, jpeg, patient):
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})
    first = post_image(client, auth, jpeg((10, 20, 30))).json()["image"]
    second = post_image(client, auth, jpeg((200, 40, 60))).json()["image"]

    one, two = stored_path(client, first["id"]), stored_path(client, second["id"])
    assert one != two
    assert one.exists() and two.exists()
    assert one.read_bytes() != two.read_bytes()


def test_the_photo_tree_contains_only_photos(client, auth, jpeg, patient):
    """Thumbnails stay out of the browsable tree, so a clinician sees originals."""
    client.post("/api/sessions", json={"patient_id": patient["id"], "operatory": "OP-1"})
    post_image(client, auth, jpeg())

    files = list(settings.image_dir.rglob("*"))
    assert files, "expected at least one stored photograph"
    assert all(f.is_dir() or f.suffix.lower() in {".jpg", ".jpeg", ".png"} for f in files)
    assert not any("thumb" in str(f).lower() for f in files)
