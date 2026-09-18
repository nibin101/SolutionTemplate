"""Adding patients, and attaching photographs to them by hand."""

from __future__ import annotations


def new_patient(client, chart="CS-2001", first="Nila", last="Rajan"):
    return client.post(
        "/api/patients",
        json={"chart_number": chart, "first_name": first, "last_name": last,
              "date_of_birth": "1993-02-11"},
    )


def test_a_patient_can_be_added(client):
    response = new_patient(client)
    assert response.status_code == 201

    body = response.json()
    assert body["display_name"] == "Nila Rajan"
    assert body["chart_number"] == "CS-2001"
    assert [p["id"] for p in client.get("/api/patients").json()] == [body["id"]]


def test_chart_numbers_are_unique(client):
    new_patient(client)
    clash = new_patient(client, first="Someone", last="Else")
    assert clash.status_code == 409


def test_patients_can_be_searched(client):
    new_patient(client)
    new_patient(client, chart="CS-2002", first="Arun", last="Kumar")

    assert len(client.get("/api/patients?q=Nila").json()) == 1
    assert len(client.get("/api/patients?q=CS-2002").json()) == 1
    assert len(client.get("/api/patients").json()) == 2


def test_photos_can_be_uploaded_to_a_patient(client, jpeg):
    patient = new_patient(client).json()

    response = client.post(
        f"/api/patients/{patient['id']}/images",
        files=[
            # Deliberately not named after a view (e.g. "frontal.jpg") - this
            # test is about the upload mechanics, not the quality gate's view
            # classification, and that name would trigger a face requirement.
            ("files", ("shot-1.jpg", jpeg((10, 20, 30)), "image/jpeg")),
            ("files", ("shot-2.jpg", jpeg((200, 40, 60)), "image/jpeg")),
        ],
    )
    assert response.status_code == 201

    body = response.json()
    assert body["stored"] == 2
    assert body["duplicates"] == 0
    assert body["rejected"] == []

    charted = client.get(f"/api/patients/{patient['id']}/images").json()
    assert len(charted) == 2
    assert {image["source"] for image in charted} == {"upload"}
    assert all(image["status"] == "assigned" for image in charted)


def test_uploading_the_same_photo_twice_does_not_duplicate_it(client, jpeg):
    """Manual upload goes through the same dedupe as the bridge."""
    patient = new_patient(client).json()
    payload = jpeg()

    first = client.post(
        f"/api/patients/{patient['id']}/images",
        files=[("files", ("a.jpg", payload, "image/jpeg"))],
    ).json()
    second = client.post(
        f"/api/patients/{patient['id']}/images",
        files=[("files", ("a-copy.jpg", payload, "image/jpeg"))],
    ).json()

    assert first["stored"] == 1
    assert second["stored"] == 0 and second["duplicates"] == 1
    assert len(client.get(f"/api/patients/{patient['id']}/images").json()) == 1


def test_upload_to_an_unknown_patient_is_refused(client, jpeg):
    response = client.post(
        "/api/patients/does-not-exist/images",
        files=[("files", ("a.jpg", jpeg(), "image/jpeg"))],
    )
    assert response.status_code == 404


def test_a_patient_reports_where_their_photos_live(client):
    patient = new_patient(client).json()
    folder = client.get(f"/api/patients/{patient['id']}/folder").json()

    assert folder["folder"] == "Nila_Rajan_CS-2001"
    assert folder["path"].endswith("Nila_Rajan_CS-2001")


def test_upload_rejects_an_oversized_batch(client, jpeg):
    patient = new_patient(client).json()
    files = [("files", (f"{i}.jpg", jpeg((i, i, i)), "image/jpeg")) for i in range(51)]

    response = client.post(f"/api/patients/{patient['id']}/images", files=files)
    assert response.status_code == 400
