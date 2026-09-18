"""The Wi-Fi camera path: a body pushing frames to the bridge over FTP.

Driven through a real socket with `ftplib` rather than by calling the handler
directly - the thing worth proving is that something speaking plain FTP, which
is all a camera does, ends up emitting a CapturedImage.
"""

from __future__ import annotations

import io
import socket
import threading
from ftplib import FTP, error_perm
from pathlib import Path

import pytest

from bridge.models import CapturedImage
from bridge.sources.ftp_receive import FtpReceiveSource

pytest.importorskip("pyftpdlib")

PASSWORD = "test-password"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def jpeg_bytes(colour: tuple[int, int, int] = (200, 120, 120)) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 48), colour)
    draw = ImageDraw.Draw(image)
    contrast = tuple(255 - channel for channel in colour)
    for y in range(0, 48, 8):
        for x in range(0, 64, 8):
            if (x // 8 + y // 8) % 2 == 0:
                draw.rectangle((x, y, x + 7, y + 7), fill=contrast)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class RunningSource:
    """Starts the source on a background thread, like the bridge supervisor."""

    def __init__(self, root: Path, port: int) -> None:
        self.source = FtpReceiveSource()
        self.source.host = "127.0.0.1"
        self.source.port = port
        self.source.user = "camera"
        self.source.password = PASSWORD
        self.source.root = root
        self.emitted: list[CapturedImage] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.source.run, args=(self.stop, self.emitted.append), daemon=True
        )

    def __enter__(self) -> "RunningSource":
        self.thread.start()
        deadline = threading.Event()
        # Wait for the listener rather than sleeping a fixed amount.
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.source.port), timeout=0.2):
                    return self
            except OSError:
                deadline.wait(0.05)
        raise AssertionError("FTP source never started listening")

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=5)

    def wait_for(self, count: int, timeout: float = 5.0) -> None:
        waiter = threading.Event()
        elapsed = 0.0
        while elapsed < timeout:
            if len(self.emitted) >= count:
                return
            waiter.wait(0.05)
            elapsed += 0.05
        raise AssertionError(f"expected {count} capture(s), got {len(self.emitted)}")


@pytest.fixture()
def running(tmp_path):
    with RunningSource(tmp_path / "incoming", free_port()) as running:
        yield running


def store(port: int, name: str, payload: bytes, user: str = "camera",
          password: str = PASSWORD) -> None:
    with FTP() as ftp:
        ftp.connect("127.0.0.1", port, timeout=5)
        ftp.login(user, password)
        ftp.storbinary(f"STOR {name}", io.BytesIO(payload))


def test_a_pushed_frame_becomes_a_capture(running):
    payload = jpeg_bytes()
    store(running.source.port, "IMG_0001.JPG", payload)
    running.wait_for(1)

    captured = running.emitted[0]
    assert captured.filename == "IMG_0001.JPG"
    assert captured.data == payload, "the bytes charted must be the bytes shot"
    assert captured.source == "ftp"


def test_received_files_do_not_pile_up_on_the_bridge(running):
    """The backend's patient folders are the store of record, not this PC."""
    store(running.source.port, "IMG_0002.JPG", jpeg_bytes((90, 160, 200)))
    running.wait_for(1)

    leftovers = [p for p in running.source.root.rglob("*") if p.is_file()]
    assert leftovers == [], f"receive directory still holds {leftovers}"


def test_non_images_are_ignored(running):
    """Bodies also push sidecars and thumbnails; they are not clinical images."""
    store(running.source.port, "IMG_0003.XMP", b"<x:xmpmeta/>")
    store(running.source.port, "IMG_0003.JPG", jpeg_bytes((140, 90, 190)))
    running.wait_for(1)

    assert [c.filename for c in running.emitted] == ["IMG_0003.JPG"]


def test_a_wrong_password_cannot_push(running):
    with pytest.raises(error_perm):
        store(running.source.port, "IMG_0004.JPG", jpeg_bytes(), password="wrong")
    assert running.emitted == []


def test_passive_data_ports_stay_inside_the_configured_range(running):
    """The firewall rule is written against this range, so it has to hold.

    Cameras transfer in passive mode. If the data port were left to the OS it
    would be a random ephemeral one per transfer, the clinic firewall could not
    name it, and every transfer would log in fine and then stall.
    """
    low, high = running.source.passive_ports

    with FTP() as ftp:
        ftp.connect("127.0.0.1", running.source.port, timeout=5)
        ftp.login("camera", PASSWORD)
        ftp.set_pasv(True)
        host, port = ftp.makepasv()
        assert low <= port <= high, f"data port {port} outside {low}-{high}"

        # And a real transfer still completes over that channel.
        ftp.storbinary("STOR IMG_0005.JPG", io.BytesIO(jpeg_bytes((10, 200, 90))))

    running.wait_for(1)
    assert running.emitted[0].filename == "IMG_0005.JPG"


def test_a_bad_port_range_falls_back_instead_of_killing_the_bridge():
    from bridge.config import _port_range

    assert _port_range("50000-50100") == (50000, 50100)
    assert _port_range("  51000 - 51010 ") == (51000, 51010)
    assert _port_range("nonsense") == (50000, 50100)
    assert _port_range("70000-80000") == (50000, 50100), "above the port space"
    assert _port_range("100-200") == (50000, 50100), "privileged ports need admin"
    assert _port_range("50100-50000") == (50000, 50100), "reversed range"


def test_source_refuses_to_run_without_a_password(tmp_path):
    """An anonymous drop box on a practice network is never acceptable."""
    source = FtpReceiveSource()
    source.password = ""
    source.root = tmp_path
    usable, detail = source.probe()
    assert usable is False
    assert "FTP_PASSWORD" in detail


def test_probe_is_happy_once_configured(tmp_path):
    source = FtpReceiveSource()
    source.password = PASSWORD
    source.root = tmp_path / "incoming"
    usable, detail = source.probe()
    assert usable is True
    assert source.root.exists(), "probe should create the landing directory"


def test_an_idle_listener_does_not_claim_to_be_a_camera(running):
    """A waiting socket is not an attached camera.

    The chair-side status bar shows the first source reporting a device, so a
    listener that always named one read as "Camera ready - FTP 0.0.0.0:2121"
    with nothing plugged in, and displaced the real camera when one was.
    """
    assert running.source.status().device is None


def test_the_device_names_the_camera_that_pushed(running):
    store(running.source.port, "IMG_0006.JPG", jpeg_bytes((120, 60, 200)))
    running.wait_for(1)

    device = running.source.status().device
    assert device is not None, "once a camera has sent something, name it"
    assert "127.0.0.1" in device, f"should carry the peer address, got {device!r}"


def test_the_advertised_address_is_one_a_human_can_type(tmp_path):
    """0.0.0.0 is the bind wildcard - useless in a camera's FTP settings."""
    from bridge.sources.ftp_receive import _reachable_address

    assert _reachable_address("0.0.0.0") != "0.0.0.0"
    assert _reachable_address("192.168.1.50") == "192.168.1.50"

    source = FtpReceiveSource()
    source.password = PASSWORD
    source.root = tmp_path / "incoming"
    source.host = "0.0.0.0"
    _, detail = source.probe()
    assert "0.0.0.0" not in detail, f"bind wildcard leaked into the UI: {detail!r}"
