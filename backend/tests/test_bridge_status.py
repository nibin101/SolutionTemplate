"""Bridge liveness as the chair-side UI sees it."""

from __future__ import annotations


def test_status_is_offline_before_any_heartbeat(client):
    status = client.get("/api/bridge/status").json()
    assert status["online"] is False
    assert status["sources"] == []


def test_heartbeat_requires_a_token(client):
    assert client.post("/api/bridge/heartbeat", json={"queue": {}}).status_code == 401


def test_heartbeat_makes_the_bridge_visible(client, auth):
    payload = {
        "version": "1.0.0",
        "operatory": "OP-1",
        "paused": False,
        "queue": {"pending": 2, "failed": 0, "delivered": 7},
        "sources": [{"name": "wpd", "available": True, "detail": "EOS R6", "captures": 7}],
    }
    reply = client.post("/api/bridge/heartbeat", headers=auth, json=payload).json()
    assert reply == {"ok": True, "window": None}, "nobody is in the chair yet"

    status = client.get("/api/bridge/status").json()
    assert status["online"] is True
    assert status["queue"]["pending"] == 2
    assert status["sources"][0]["name"] == "wpd"
