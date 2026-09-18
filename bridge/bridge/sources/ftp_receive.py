"""Receive photographs pushed over Wi-Fi by the camera itself.

Every current mirrorless and DSLR body with wireless - Canon R, Nikon Z, Sony
Alpha, Fujifilm X-H - can be told to FTP each frame to an address as it is shot.
That is the one wireless path the manufacturers all agree on, it needs no vendor
software on the clinic PC, and it needs nothing installed on the camera.

This source *is* the FTP server. Point the body at this PC's address and every
shutter press lands here within a second or two, goes through the same dedupe,
spool, retry and charting pipeline as a USB capture, and is deleted from the
receive directory immediately - the backend's patient folders are the store of
record, and the bridge must never become a second unmanaged pile of clinical
photographs.

Why not just watch a folder
---------------------------
`folder_watch` can already pick up a camera's Wi-Fi drop, but only once someone
else has put the file on disk - a vendor utility, or an SMB share the camera
writes to. Both are extra moving parts that a practice has to install, configure
and keep running, and a share exposes a writable directory to the whole network.
Worse, a folder watcher has to *guess* when a file is finished; this gets told,
by the protocol, in `on_file_received`. A photo can never be read half-written.

Security
--------
Opening a listening socket in a clinic is not free, so: the source refuses to
start without a password (an accidental anonymous drop box on a medical network
would be indefensible), and the receive directory is emptied as fast as files
arrive. It is off unless `ftp` is named in ENABLED_SOURCES.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from ..config import config
from ..models import CapturedImage
from .base import CaptureSource, Emit
from .fileutil import file_mtime_iso


def _reachable_address(host: str) -> str:
    """An address a human can actually type into a camera.

    `0.0.0.0` is the wildcard that means "bind every interface". It is the
    right thing to listen on and a useless thing to show anyone - nobody can
    enter it into a camera's FTP settings. Resolve it to this machine's
    address on the practice network instead.
    """
    if host not in ("0.0.0.0", "::", ""):
        return host
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Nothing is sent; this only asks the routing table which interface
        # would be used to reach the outside world.
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "this PC"
    finally:
        probe.close()


class FtpReceiveSource(CaptureSource):
    name = "ftp"
    description = "Receives photos pushed over Wi-Fi by the camera's own FTP transfer"

    def __init__(self) -> None:
        super().__init__()
        self.host = config.ftp_host
        self.port = config.ftp_port
        self.user = config.ftp_user
        self.password = config.ftp_password
        self.root = config.ftp_root
        self.passive_ports = config.ftp_passive_ports

    # -- readiness ---------------------------------------------------------

    def probe(self) -> tuple[bool, str]:
        try:
            import pyftpdlib  # noqa: F401
        except ImportError:
            return False, "pyftpdlib is not installed"
        if not self.password:
            # Refusing here rather than falling back to anonymous: a silent
            # open drop box on a practice network is the wrong failure.
            return False, "FTP_PASSWORD is not set"
        self.root.mkdir(parents=True, exist_ok=True)
        low, high = self.passive_ports
        return True, (f"ready on {_reachable_address(self.host)}:{self.port} "
                      f"(data {low}-{high})")

    # -- running -----------------------------------------------------------

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer

        self.root.mkdir(parents=True, exist_ok=True)

        authorizer = DummyAuthorizer()
        # Full rights inside the receive directory only. Cameras differ wildly
        # in what they do before a STOR - some mkdir a DCIM tree, some rename,
        # some list first - and the directory is drained continuously, so there
        # is nothing here to protect beyond the few seconds a file exists.
        authorizer.add_user(self.user, self.password, str(self.root), perm="elradfmw")

        source = self

        low, high = self.passive_ports

        class _Handler(FTPHandler):
            authorizer = None  # set below; kept explicit for readability
            banner = "SnapChart camera bridge ready"
            # Cameras transfer in passive mode: they open the control channel to
            # our port, then ask which port to send the *data* on. Left as None
            # that answer is a random ephemeral port, so a clinic firewall can
            # only be opened by allowing the whole program - and until someone
            # does, every transfer logs in fine and then stalls. Pinning the
            # range makes the firewall rule two lines and the failure impossible.
            passive_ports = range(low, high + 1)

            def on_file_received(self, file: str) -> None:
                # The peer address is the only identity a camera offers over
                # FTP - there is no model string in the protocol - so it is
                # what names the device once one has actually sent something.
                source._ingest(Path(file), emit, self.remote_ip)

            def on_incomplete_file_received(self, file: str) -> None:
                # The camera dropped out mid-frame. A truncated JPEG is not a
                # clinical photograph; the body re-sends on its own.
                source.log.warning("incomplete transfer discarded: %s", file)
                Path(file).unlink(missing_ok=True)

            def on_login_failed(self, username: str, password: str) -> None:
                source.log.warning("rejected FTP login for %r", username)

        _Handler.authorizer = authorizer

        server = FTPServer((self.host, self.port), _Handler)
        # Deliberately no `device`: this is a socket waiting, not a camera
        # attached. Claiming one made the chair-side pill read "Camera ready -
        # FTP 0.0.0.0:2121" whenever nothing was plugged in, because the UI
        # shows the first source reporting a device - so an idle listener
        # masqueraded as a live camera and displaced the real one.
        self.note(f"waiting on {_reachable_address(self.host)}:{self.port} "
                  f"as {self.user}", available=True, device=None)
        self.log.info("FTP receiver listening on %s:%s (root=%s)",
                      self.host, self.port, self.root)

        # serve_forever() is called exactly once, on its own thread, and is
        # unblocked by close_all() below. Polling it with blocking=False would
        # be the obvious way to watch `stop_event` instead - but pyftpdlib
        # re-emits its ">>> starting FTP server <<<" banner on every call
        # regardless of the blocking flag, which would write that line to the
        # bridge log twice a second for as long as the clinic is open.
        serving = threading.Thread(
            target=server.serve_forever,
            kwargs={"timeout": 0.5, "handle_exit": False},
            name="snapchart-ftp",
            daemon=True,
        )
        serving.start()

        try:
            while not stop_event.is_set():
                stop_event.wait(0.5)
        finally:
            server.close_all()
            serving.join(timeout=5)
            self.note("stopped", available=False)

    # -- one received frame ------------------------------------------------

    def _ingest(self, path: Path, emit: Emit, remote_ip: str | None = None) -> None:
        if not config.is_image(path.name):
            # Bodies also push XMP sidecars, .THM thumbnails and the odd log.
            self.log.debug("ignoring non-image upload %s", path.name)
            path.unlink(missing_ok=True)
            return

        try:
            data = path.read_bytes()
        except OSError as exc:
            self.log.error("could not read received file %s: %s", path, exc)
            return

        emit(
            CapturedImage(
                filename=path.name,
                data=data,
                source=self.name,
                captured_at=file_mtime_iso(path),
                origin=str(path),
            )
        )
        self.count_capture()
        self.note(f"last capture: {path.name}", available=True,
                  device=f"Wi-Fi camera {remote_ip}" if remote_ip else "Wi-Fi camera")

        # The bytes are in the spool now, which is what survives a restart.
        # Leaving a copy here would build a second, unmanaged pile of patient
        # photographs on the clinic PC.
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning("received %s but could not remove it: %s", path.name, exc)
