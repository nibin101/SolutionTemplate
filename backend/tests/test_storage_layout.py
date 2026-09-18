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


# -- move_image reliability -------------------------------------------------


def test_move_image_retries_a_transient_lock(monkeypatch, tmp_path):
    """A file held open for a moment (antivirus, an indexer) must not be
    treated as a permanent failure - the classic Windows trigger this guards
    against is a scan finishing a beat after the assign click."""
    from app import storage

    source = tmp_path / "src" / "shot.jpg"
    source.parent.mkdir()
    source.write_bytes(b"fake-jpeg-bytes")

    calls = {"n": 0}
    real_move = storage.shutil.move

    def flaky_move(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("file in use")
        return real_move(src, dst)

    monkeypatch.setattr(storage.shutil, "move", flaky_move)
    monkeypatch.setattr(storage.time, "sleep", lambda _seconds: None)

    result = storage.move_image(source, "Patient_CS-1")
    assert result.moved is True
    assert result.error is None
    assert result.path.exists()
    assert calls["n"] == 3


def test_move_image_reports_failure_instead_of_lying(monkeypatch, tmp_path):
    """When every retry is exhausted, the caller must be told - not handed
    back the old path relabelled as a success."""
    from app import storage

    source = tmp_path / "src" / "shot.jpg"
    source.parent.mkdir()
    source.write_bytes(b"fake-jpeg-bytes")

    def always_locked(src, dst):
        raise PermissionError("file in use")

    monkeypatch.setattr(storage.shutil, "move", always_locked)
    monkeypatch.setattr(storage.time, "sleep", lambda _seconds: None)

    result = storage.move_image(source, "Patient_CS-1")
    assert result.moved is False
    assert result.error is not None
    assert result.path == source           # unchanged: still where it was
    assert source.exists()                 # and the bytes are not lost


def test_assign_surfaces_a_warning_when_the_move_fails(monkeypatch, client, auth, patient, jpeg):
    """The API must not report a clean success when the file never actually
    reached the patient's folder - a clinician acting on "it's filed" needs
    to know when that is not true yet."""
    ingest = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("stuck.jpg", jpeg(), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1"},
    )
    image_id = ingest.json()["image"]["id"]

    from app.routers import images as images_router
    from app.storage import MoveResult

    def always_fails(current, folder):
        return MoveResult(current, moved=False, error="file in use")

    monkeypatch.setattr(images_router, "move_image", always_fails)

    response = client.post(f"/api/images/{image_id}/assign", json={"patient_id": patient["id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["patient_id"] == patient["id"]   # ownership still recorded
    assert "warning" in body and "could not be moved" in body["warning"]


# -- the folder tree endpoint -------------------------------------------


def test_folder_tree_groups_by_patient_and_date(client, auth, patient, jpeg):
    client.post(
        "/api/ingest", headers=auth,
        files={"file": ("a.jpg", jpeg((1, 2, 3)), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1"},
    )
    unassigned_id = client.get("/api/images/unassigned").json()[0]["id"]
    client.post(f"/api/images/{unassigned_id}/assign", json={"patient_id": patient["id"]})

    tree = client.get("/api/images/folders").json()
    assert len(tree) == 1
    folder = tree[0]
    assert folder["patient_id"] == patient["id"]
    assert folder["display_name"] == f"{patient['first_name']} {patient['last_name']} ({patient['chart_number']})"
    assert folder["image_count"] == 1
    assert len(folder["dates"]) == 1
    assert folder["dates"][0]["images"][0]["id"] == unassigned_id


def test_folder_tree_puts_unassigned_last_and_separately_from_any_patient(client, auth, patient, jpeg):
    client.post(
        "/api/ingest", headers=auth,
        files={"file": ("assigned.jpg", jpeg((4, 5, 6)), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1", "patient_id": ""},
    )
    unassigned_id = client.get("/api/images/unassigned").json()[0]["id"]
    client.post(f"/api/images/{unassigned_id}/assign", json={"patient_id": patient["id"]})

    client.post(
        "/api/ingest", headers=auth,
        files={"file": ("still-unassigned.jpg", jpeg((7, 8, 9)), "image/jpeg")},
        data={"source": "test", "operatory": "OP-9"},
    )

    tree = client.get("/api/images/folders").json()
    names = [f["name"] for f in tree]
    assert names[-1] == "_unassigned"
    assert tree[-1]["patient_id"] is None
    assert tree[-1]["display_name"] == "Needs assignment"


def test_folder_tree_names_match_what_is_actually_on_disk(client, auth, patient, jpeg):
    """The whole point of this endpoint: it must never show a folder name that
    disagrees with the one the file was actually written under."""
    from pathlib import Path

    from app.storage import patient_folder

    response = client.post(
        "/api/ingest", headers=auth,
        files={"file": ("x.jpg", jpeg((11, 22, 33)), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1"},
    )
    image_id = response.json()["image"]["id"]
    client.post(f"/api/images/{image_id}/assign", json={"patient_id": patient["id"]})

    expected = patient_folder(patient["chart_number"], patient["first_name"], patient["last_name"])
    tree = client.get("/api/images/folders").json()
    assert tree[0]["name"] == expected

    stored = client.get(f"/api/images/{image_id}/file")
    assert stored.status_code == 200
