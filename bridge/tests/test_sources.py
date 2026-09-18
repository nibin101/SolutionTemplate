"""Capture sources: the contract every transport has to honour."""

from __future__ import annotations

import threading
import time

import pytest

from bridge.models import CapturedImage
from bridge.sources import BUILDERS, build_sources
from bridge.sources.folder_watch import FolderWatchSource
from bridge.sources.simulator import SimulatorSource


def collect(source, timeout: float, wanted: int = 1) -> list[CapturedImage]:
    """Run a source until it has emitted `wanted` images (or time runs out)."""
    captured: list[CapturedImage] = []
    stop = threading.Event()
    done = threading.Event()

    def emit(image: CapturedImage) -> None:
        captured.append(image)
        if len(captured) >= wanted:
            done.set()

    worker = threading.Thread(target=source.run, args=(stop, emit), daemon=True)
    worker.start()
    try:
        done.wait(timeout)
    finally:
        stop.set()
        worker.join(timeout=5)
    return captured


def test_every_registered_name_builds():
    built = build_sources(list(BUILDERS))
    assert {source.name for source in built} == set(BUILDERS)


def test_unknown_source_is_skipped_not_fatal():
    assert build_sources(["folder", "not-a-real-source"])[0].name == "folder"


def test_simulator_produces_real_jpegs():
    source = SimulatorSource(interval=0.1)
    images = collect(source, timeout=5, wanted=2)

    assert len(images) >= 2
    assert all(image.data.startswith(b"\xff\xd8\xff") for image in images), "not a JPEG"
    # Every frame is stamped, so dedupe cannot silently swallow a demo.
    assert images[0].data != images[1].data
    assert images[0].camera_model


def test_folder_watch_picks_up_a_new_file(tmp_path):
    from PIL import Image

    source = FolderWatchSource(folders=[tmp_path])
    captured: list[CapturedImage] = []
    stop = threading.Event()
    seen = threading.Event()

    def emit(image: CapturedImage) -> None:
        captured.append(image)
        seen.set()

    worker = threading.Thread(target=source.run, args=(stop, emit), daemon=True)
    worker.start()
    try:
        time.sleep(0.6)  # let the observer settle before the file lands
        Image.new("RGB", (32, 24), (90, 140, 200)).save(tmp_path / "IMG_9001.JPG")
        assert seen.wait(20), "watcher did not report the new image"
    finally:
        stop.set()
        worker.join(timeout=10)

    assert captured[0].filename == "IMG_9001.JPG"
    assert captured[0].source == "folder"
    assert captured[0].data.startswith(b"\xff\xd8\xff")


def test_folder_watch_ignores_non_images(tmp_path):
    source = FolderWatchSource(folders=[tmp_path])
    (tmp_path / "notes.txt").write_text("not a photo")
    ok, detail = source.probe()
    assert ok and str(tmp_path) in detail


@pytest.mark.parametrize("name", ["nikon", "sony"])
def test_unimplemented_vendor_adapters_say_so_instead_of_pretending(name):
    [source] = build_sources([name])
    available, detail = source.probe()
    assert available is False
    assert "wpd" in detail or "SDK" in detail


def test_canon_adapter_reports_a_missing_sdk_clearly():
    [source] = build_sources(["canon"])
    available, detail = source.probe()
    # Without EDSDK.dll configured this must degrade, not crash the bridge.
    if not available:
        assert "CANON_EDSDK_DLL" in detail or "EDSDK" in detail


# --- status reporting ------------------------------------------------------

def _probe_source():
    from bridge.sources.base import CaptureSource

    class _Probe(CaptureSource):
        name = "probe"

        def run(self, stop_event, emit):  # pragma: no cover - not exercised
            raise NotImplementedError

    return _Probe()


def test_note_leaves_the_device_alone_when_not_given():
    source = _probe_source()
    source.note("scanning", available=True, device="Canon EOS 90D")
    source.note("last capture: IMG_1.JPG", available=True)
    assert source.status().device == "Canon EOS 90D"


def test_note_clears_the_device_when_told_to():
    """'No camera connected' must not keep advertising the last one.

    The status bar read "Camera ready - <last phone>" with nothing plugged in,
    because None meant both 'not supplied' and 'clear it'.
    """
    source = _probe_source()
    source.note("scanning", available=True, device="Nothing A142")
    source.note("no camera connected", available=True, device=None)
    assert source.status().device is None


def test_note_can_set_a_device():
    source = _probe_source()
    source.note("connected", available=True, device="Nikon Z6")
    assert source.status().device == "Nikon Z6"
