"""The reliability guarantees the spool is supposed to provide."""

from __future__ import annotations

from bridge.models import CapturedImage


def capture(payload: bytes = b"fake-jpeg-bytes", name: str = "IMG_0001.JPG") -> CapturedImage:
    return CapturedImage(filename=name, data=payload, source="test")


def test_capture_survives_on_disk_before_any_upload(spool):
    spool.enqueue(capture())
    [item] = spool.due_items()
    assert item.blob_path.exists()
    assert item.read_bytes() == b"fake-jpeg-bytes"


def test_identical_bytes_are_queued_once(spool):
    """A re-scanned card or a re-fired watcher event must not duplicate a photo."""
    assert spool.enqueue(capture()) is not None
    assert spool.enqueue(capture(name="COPY.JPG")) is None
    assert spool.stats()["pending"] == 1


def test_delivered_bytes_are_never_queued_again(spool):
    spool.enqueue(capture())
    [item] = spool.due_items()
    spool.mark_sent(item)

    assert spool.enqueue(capture()) is None
    assert spool.stats() == {"pending": 0, "failed": 0, "delivered": 1}
    assert not item.blob_path.exists(), "a delivered blob should not keep using disk"


def test_retries_back_off_then_give_up(spool):
    spool.enqueue(capture())
    [item] = spool.due_items()

    spool.mark_retry(item, "connection refused")
    assert spool.stats()["pending"] == 1
    # The backoff is in the future, so the uploader will not spin on it.
    assert spool.due_items() == []

    # max_attempts is 3 in the fixture: two more failures park the item.
    for attempt in (1, 2):
        item.attempts = attempt
        spool.mark_retry(item, "connection refused")

    stats = spool.stats()
    assert stats == {"pending": 0, "failed": 1, "delivered": 0}
    assert spool.failures()[0]["filename"] == "IMG_0001.JPG"


def test_failed_items_keep_their_bytes_and_can_be_retried(spool):
    spool.enqueue(capture())
    [item] = spool.due_items()
    spool.mark_failed(item, "backend refused")

    assert item.blob_path.exists(), "a failed capture must never be thrown away"
    assert spool.retry_failed() == 1
    assert len(spool.due_items()) == 1


def test_queue_survives_a_restart(spool, tmp_path):
    from bridge.spool import Spool

    spool.enqueue(capture())
    reopened = Spool(directory=tmp_path, max_attempts=3)
    assert reopened.stats()["pending"] == 1
