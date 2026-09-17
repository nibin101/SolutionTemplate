"""System-tray face of the background service.

The bridge is meant to be invisible - started at login, running all day, noticed
by nobody. But "invisible" must not mean "unaccountable": the tray icon changes
colour with the health of the link and its menu carries the three actions a
practice actually needs (open the chart view, pause capture, retry failures).
"""

from __future__ import annotations

import threading
import webbrowser

from PIL import Image, ImageDraw

from .app import BridgeApp
from .config import config
from .logging_setup import setup_logging

log = setup_logging().getChild("tray")

ICON_SIZE = 64
REFRESH_SECONDS = 3.0

GREEN = (46, 189, 133)
AMBER = (232, 165, 51)
RED = (214, 77, 77)
GREY = (140, 145, 150)


def _icon_image(colour: tuple[int, int, int]) -> Image.Image:
    """A camera-shutter dot: readable at 16px, which is all a tray icon gets."""
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, ICON_SIZE - 2, ICON_SIZE - 2), fill=colour)
    draw.ellipse((20, 20, ICON_SIZE - 20, ICON_SIZE - 20), fill=(255, 255, 255))
    return image


def _state_colour(status: dict) -> tuple[int, int, int]:
    if status["paused"]:
        return GREY
    if status["queue"]["failed"]:
        return RED
    if status["last_error"] or status["queue"]["pending"]:
        return AMBER
    if not any(source["available"] for source in status["sources"]):
        return RED
    return GREEN


def _summary(status: dict) -> str:
    queue = status["queue"]
    if status["paused"]:
        return "Capture paused"
    if queue["failed"]:
        return f"{queue['failed']} upload(s) failed"
    if queue["pending"]:
        return f"{queue['pending']} image(s) uploading"
    if status["last_error"]:
        return "Backend unreachable"
    return f"{queue['delivered']} image(s) charted"


def run_tray(app: BridgeApp) -> None:
    """Run the tray icon on the main thread; returns when the user quits."""
    import pystray

    def toggle_pause(icon, item) -> None:
        if app.paused.is_set():
            app.paused.clear()
            log.info("capture resumed")
        else:
            app.paused.set()
            log.info("capture paused")
        icon.update_menu()

    def retry_failed(icon, item) -> None:
        count = app.spool.retry_failed()
        log.info("retry requested from tray (%d item(s))", count)
        icon.update_menu()

    def open_ui(icon, item) -> None:
        webbrowser.open(config.ui_url)

    def open_logs(icon, item) -> None:
        import os

        os.startfile(config.log_dir)  # noqa: S606 - opening our own log folder

    def quit_bridge(icon, item) -> None:
        icon.visible = False
        icon.stop()

    def source_lines() -> str:
        status = app.status()
        lines = [f"{s['name']}: {s['detail']}" for s in status["sources"]]
        return " | ".join(lines) if lines else "no sources enabled"

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: _summary(app.status()), None, enabled=False),
        pystray.MenuItem(lambda item: source_lines(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open chair-side view", open_ui, default=True),
        pystray.MenuItem("Pause capture", toggle_pause,
                         checked=lambda item: app.paused.is_set()),
        pystray.MenuItem("Retry failed uploads", retry_failed),
        pystray.MenuItem("Open log folder", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit SnapChart bridge", quit_bridge),
    )

    icon = pystray.Icon(
        "snapchart-bridge",
        _icon_image(GREY),
        f"SnapChart bridge - {config.operatory}",
        menu,
    )

    def refresh() -> None:
        while not app.stop_event.is_set():
            try:
                status = app.status()
                icon.icon = _icon_image(_state_colour(status))
                icon.title = f"SnapChart {config.operatory} - {_summary(status)}"
            except Exception as exc:
                log.debug("tray refresh failed: %s", exc)
            app.stop_event.wait(REFRESH_SECONDS)

    threading.Thread(target=refresh, name="tray-refresh", daemon=True).start()
    icon.run()
