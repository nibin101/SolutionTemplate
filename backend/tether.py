"""
tether.py — USB tethering mode via python-gphoto2 (libgphoto2).

In tether mode this module connects to a USB-attached DSLR (Canon, Nikon, Sony,
or any PTP-compatible camera) and listens for new captures. When the shutter is
fired, the camera notifies us via a GP_EVENT_FILE_ADDED event and we download
the image directly to the inbox directory, from where the normal ingest pipeline
picks it up.

Supported cameras: any camera listed at http://gphoto.org/proj/libgphoto2/support.php

System requirements:
  sudo apt install libgphoto2-dev gphoto2
  pip install gphoto2

On Linux, if GNOME auto-mounts the camera as a media device, it will block
gphoto2 access. Run: sudo systemctl stop gvfs-gphoto2-volume-monitor
  or: sudo pkill gvfsd-gphoto2
before starting the service.
"""

import logging
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def _run_tether_loop(inbox: Path, on_new_file: Callable[[Path], None], stop_event: threading.Event) -> None:
    """
    Main tethering loop. Runs in a background daemon thread.

    Blocks on camera.wait_for_event() — a 1-second timeout prevents the loop
    from hanging indefinitely if the camera is disconnected.
    """
    try:
        import gphoto2 as gp  # type: ignore
    except ImportError:
        logger.error(
            "python-gphoto2 is not installed. "
            "Run: sudo apt install libgphoto2-dev gphoto2 && pip install gphoto2"
        )
        return

    logger.info("Tether: initialising camera connection…")
    camera = gp.Camera()

    try:
        camera.init()
    except gp.GPhoto2Error as exc:
        logger.error("Tether: camera init failed — %s", exc)
        logger.error("Is the camera connected and powered on? Is gvfsd-gphoto2 blocking the port?")
        return

    logger.info("Tether: camera connected — waiting for captures…")

    while not stop_event.is_set():
        try:
            # wait_for_event blocks for up to timeout_ms milliseconds.
            # Using 1000 ms gives us a responsive stop_event check without
            # spinning the CPU.
            event_type, event_data = camera.wait_for_event(1000)

            if event_type == gp.GP_EVENT_FILE_ADDED:
                # A new image was captured and added to the camera's memory
                cam_folder: str = event_data.folder
                cam_filename: str = event_data.name
                logger.info("Tether: capture received — %s%s", cam_folder, cam_filename)

                # Download the file from the camera into the inbox
                dest = inbox / cam_filename
                _download_file(camera, cam_folder, cam_filename, dest)
                on_new_file(dest)

            elif event_type == gp.GP_EVENT_CAMERA_EXIT:
                logger.warning("Tether: camera disconnected — stopping tether loop")
                break

        except gp.GPhoto2Error as exc:
            logger.error("Tether: gphoto2 error — %s", exc)
            break

    try:
        camera.exit()
    except Exception:
        pass

    logger.info("Tether: loop stopped")


def _download_file(camera, folder: str, filename: str, destination: Path) -> None:
    """Download a single file from the camera to the local inbox."""
    try:
        import gphoto2 as gp  # type: ignore

        destination.parent.mkdir(parents=True, exist_ok=True)
        cam_file = camera.file_get(folder, filename, gp.GP_FILE_TYPE_NORMAL)
        cam_file.save(str(destination))
        logger.info("Tether: downloaded → %s", destination.name)
    except Exception as exc:
        logger.error("Tether: download failed for %s — %s", filename, exc)


def start_tether(inbox: Path, on_new_file: Callable[[Path], None]) -> threading.Thread:
    """
    Start the USB tethering loop in a background daemon thread.

    Returns the thread so the caller can monitor it. The thread will exit
    cleanly when the stop_event is set (call stop_tether) or if the camera
    disconnects.
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_tether_loop,
        args=(inbox, on_new_file, stop_event),
        daemon=True,
        name="tether-loop",
    )
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    logger.info("Tether thread started")
    return thread


def stop_tether(thread: threading.Thread) -> None:
    """Signal the tether loop to stop and wait for it to exit."""
    if hasattr(thread, "stop_event"):
        thread.stop_event.set()
    thread.join(timeout=5.0)
    logger.info("Tether thread stopped")
