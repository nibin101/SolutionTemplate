"""Uploader behaviour, without touching the network."""

from __future__ import annotations

import threading

from bridge.models import CapturedImage
from bridge.uploader import Uploader

import requests


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def make_uploader(spool, responder) -> Uploader:
    uploader = Uploader(spool, threading.Event())
    uploader.session.post = responder  # type: ignore[method-assign]
    return uploader


def enqueue(spool, payload: bytes = b"bytes") -> None:
    spool.enqueue(CapturedImage(filename="IMG_1.JPG", data=payload, source="test"))


def test_successful_upload_clears_the_queue(spool):
    sent = {}

    def responder(url, **kwargs):
        sent["url"] = url
        sent["token"] = kwargs["headers"]["X-Bridge-Token"]
        sent["form"] = kwargs["data"]
        return FakeResponse(200, {"status": "stored", "image": {"id": "abc"}})

    uploader = make_uploader(spool, responder)
    enqueue(spool)
    uploader._deliver(spool.due_items()[0])

    assert spool.stats() == {"pending": 0, "failed": 0, "delivered": 1}
    assert sent["url"].endswith("/api/ingest")
    assert sent["form"]["content_hash"]
    assert sent["token"]


def test_duplicate_response_counts_as_delivered(spool):
    """The backend already had these bytes - that is success, not failure."""
    uploader = make_uploader(spool, lambda url, **kw: FakeResponse(200, {"status": "duplicate"}))
    enqueue(spool)
    uploader._deliver(spool.due_items()[0])

    assert spool.stats()["delivered"] == 1


def test_network_failure_is_retried_not_dropped(spool):
    def responder(url, **kwargs):
        raise requests.ConnectionError("backend down")

    uploader = make_uploader(spool, responder)
    enqueue(spool)
    item = spool.due_items()[0]
    uploader._deliver(item)

    assert spool.stats()["pending"] == 1
    assert item.blob_path.exists()
    assert uploader.online is False


def test_permanent_rejection_is_not_retried_forever(spool):
    uploader = make_uploader(spool, lambda url, **kw: FakeResponse(415, text="unsupported"))
    enqueue(spool)
    uploader._deliver(spool.due_items()[0])

    assert spool.stats()["failed"] == 1


def test_bad_token_keeps_the_image_and_says_why(spool):
    uploader = make_uploader(spool, lambda url, **kw: FakeResponse(401, text="nope"))
    enqueue(spool)
    uploader._deliver(spool.due_items()[0])

    assert spool.stats()["pending"] == 1
    assert "BRIDGE_TOKEN" in (uploader.last_error or "")


def test_view_is_forwarded_when_the_source_provides_one(spool):
    """The simulator tags each shot with its view ("Frontal retracted", ...) -
    the backend's quality gate needs it to tell an extraoral shot from an
    intraoral one without guessing from the filename, which a real camera
    never names usefully for that."""
    sent = {}

    def responder(url, **kwargs):
        sent["form"] = kwargs["data"]
        return FakeResponse(200, {"status": "stored", "image": {"id": "abc"}})

    uploader = make_uploader(spool, responder)
    spool.enqueue(
        CapturedImage(
            filename="SIM_0001.JPG", data=b"bytes", source="simulator",
            extra={"view": "Frontal retracted"},
        )
    )
    uploader._deliver(spool.due_items()[0])

    assert sent["form"]["view"] == "Frontal retracted"


def test_view_is_simply_absent_when_the_source_has_none(spool):
    """A real DSLR carries no view label - the form must not send an empty
    or placeholder value for a field the backend treats as optional."""
    sent = {}

    def responder(url, **kwargs):
        sent["form"] = kwargs["data"]
        return FakeResponse(200, {"status": "stored", "image": {"id": "abc"}})

    uploader = make_uploader(spool, responder)
    enqueue(spool)  # no `view` in extra
    uploader._deliver(spool.due_items()[0])

    assert "view" not in sent["form"]
