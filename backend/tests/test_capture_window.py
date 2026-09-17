"""The window the server hands the bridge, and the routing it does on the way back.

This replaced a command queue. The bridge no longer asks for photographs and is
no longer told who they are for: it is told when the session started and ended,
and that is enough for it to pick the right files off the camera on its own.

Two properties are worth holding on to here - that the window is correct, and
that patient identity still never leaves the server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def beat(client, auth, operatory="OP-1"):
    """One heartbeat. The reply is where the bridge learns the window."""
    return client.post(
        "/api/bridge/heartbeat",
        headers=auth,
        json={"operatory": operatory, "queue": {}, "sources": []},
    ).json()


def open_session(client, patient_id, operatory="OP-1"):
    return client.post(
        "/api/sessions", json={"patient_id": patient_id, "operatory": operatory}
    ).json()


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(
        microsecond=0
    ).isoformat()


# -- the window ------------------------------------------------------------


def test_no_session_means_no_window(client, auth, patient):
    """Nobody in the chair: the bridge is told to take nothing."""
    assert beat(client, auth)["window"] is None


def test_an_open_session_opens_the_window(client, auth, patient):
    session = open_session(client, patient["id"])

    window = beat(client, auth)["window"]
    assert window["since"] == session["started_at"]
    assert window["until"] is None, "an open session has not ended yet"


def test_closing_a_session_bounds_the_window(client, auth, patient):
    session = open_session(client, patient["id"])
    closed = client.post(f"/api/sessions/{session['id']}/end").json()

    window = beat(client, auth)["window"]
    assert window["since"] == closed["started_at"]
    assert window["until"] == closed["ended_at"]


def test_a_closed_session_keeps_its_window_briefly(client, auth, patient):
    """The camera is swept every few seconds, so the last shot is read late.

    Dropping the window the instant Stop is pressed would strand a photograph
    taken a moment before it.
    """
    session = open_session(client, patient["id"])
    client.post(f"/api/sessions/{session['id']}/end")
    assert beat(client, auth)["window"] is not None


def test_each_room_gets_its_own_window(client, auth, patient):
    open_session(client, patient["id"], operatory="OP-2")

    assert beat(client, auth, operatory="OP-1")["window"] is None
    assert beat(client, auth, operatory="OP-2")["window"] is not None


def test_the_window_never_names_the_patient(client, auth, patient):
    """The whole point: the capture PC is told when, never who."""
    open_session(client, patient["id"])
    window = beat(client, auth)["window"]

    assert set(window) == {"since", "until"}
    assert patient["id"] not in str(window)


# -- routing on the way back -----------------------------------------------


def upload(client, auth, jpeg, captured_at, colour=(10, 20, 30)):
    return client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("IMG.JPG", jpeg(colour), "image/jpeg")},
        data={"source": "wpd", "operatory": "OP-1", "captured_at": captured_at},
    ).json()


def test_a_photo_taken_during_the_session_is_charted(client, auth, jpeg, patient):
    open_session(client, patient["id"])

    body = upload(client, auth, jpeg, ago(seconds=5))
    assert body["image"]["patient_id"] == patient["id"]
    assert body["image"]["status"] == "assigned"


def test_a_photo_taken_before_the_session_is_not_charted(client, auth, jpeg, patient):
    """The server checks the timestamp too - it is the side that must be right."""
    open_session(client, patient["id"])

    body = upload(client, auth, jpeg, ago(hours=30))
    assert body["image"]["patient_id"] is None
    assert body["image"]["status"] == "unassigned"


def test_a_trailing_shot_still_reaches_the_chart_after_stop(client, auth, jpeg, patient):
    """Taken during the session, read off the camera just after it closed."""
    session = open_session(client, patient["id"])
    taken = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    client.post(f"/api/sessions/{session['id']}/end")

    body = upload(client, auth, jpeg, taken)
    assert body["image"]["patient_id"] == patient["id"]
    assert body["image"]["session_id"] == session["id"]


def test_a_shot_taken_after_stop_is_held_for_review(client, auth, jpeg, patient):
    session = open_session(client, patient["id"])
    client.post(f"/api/sessions/{session['id']}/end")

    # Well past both the window and the drift tolerance.
    body = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("LATE.JPG", jpeg((90, 90, 90)), "image/jpeg")},
        data={
            "source": "wpd",
            "operatory": "OP-1",
            "captured_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        },
    ).json()
    assert body["image"]["status"] == "unassigned"
