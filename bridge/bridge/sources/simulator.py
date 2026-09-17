"""A camera that exists only in software.

The point of this source is demo and test determinism: it drives the exact same
pipeline as a real body - emit, spool, dedupe, upload, chart - so the workflow
can be shown, and regression-tested, on a laptop with no camera attached.

If `SIMULATOR_FOLDER` contains images, they are replayed in order; otherwise a
synthetic intraoral series is generated. Either way each frame is stamped with a
shot number and timestamp, which also guarantees unique bytes so the dedupe
layer does not (correctly) discard the demo.
"""

from __future__ import annotations

import io
import threading
from datetime import datetime

from PIL import Image, ImageDraw

from ..config import config
from ..models import CapturedImage, utcnow_iso
from .base import CaptureSource, Emit

# The five views a dental photography protocol normally captures.
VIEWS = [
    "Frontal retracted",
    "Upper occlusal",
    "Lower occlusal",
    "Right buccal",
    "Left buccal",
]

FRAME_SIZE = (1200, 800)
SIM_MAKE = "SnapChart"
SIM_MODEL = "Simulated DSLR"


def _synthetic_frame(view: str) -> Image.Image:
    """A crude but recognisable stand-in for an intraoral photograph."""
    image = Image.new("RGB", FRAME_SIZE, (28, 24, 26))
    draw = ImageDraw.Draw(image)

    # Soft-tissue field.
    draw.ellipse((120, 120, 1080, 680), fill=(176, 92, 96))
    draw.ellipse((200, 210, 1000, 590), fill=(58, 30, 34))

    # Two arches of teeth.
    for row, (top, height) in enumerate(((250, 120), (430, 120))):
        for index in range(10):
            left = 240 + index * 74
            shade = 238 - (index % 3) * 9 - row * 4
            draw.rounded_rectangle(
                (left, top, left + 62, top + height),
                radius=14,
                fill=(shade, shade - 4, shade - 12),
                outline=(120, 110, 105),
            )

    draw.text((140, 60), view, fill=(240, 240, 240))
    return image


def _annotate(image: Image.Image, shot: int, view: str) -> Image.Image:
    frame = image.convert("RGB").copy()
    draw = ImageDraw.Draw(frame)
    caption = f"SIM SHOT {shot:04d}  {datetime.now().strftime('%H:%M:%S')}  {view}"
    draw.rectangle((0, frame.height - 46, frame.width, frame.height), fill=(0, 0, 0))
    draw.text((18, frame.height - 32), caption, fill=(0, 255, 170))
    return frame


def _encode(image: Image.Image) -> bytes:
    """JPEG-encode with camera-style EXIF so the chart shows a real-looking body."""
    exif = Image.Exif()
    exif[0x010F] = SIM_MAKE
    exif[0x0110] = SIM_MODEL
    try:
        exif.get_ifd(0x8769)[0x9003] = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, exif=exif)
    return buffer.getvalue()


class SimulatorSource(CaptureSource):
    name = "simulator"
    description = "Replays a sample capture series on a timer (no hardware)"

    def __init__(self, interval: float | None = None) -> None:
        super().__init__()
        self.interval = interval if interval is not None else config.simulator_interval
        self.shot = 0

    def _samples(self) -> list[Image.Image]:
        folder = config.simulator_folder
        if not folder.is_dir():
            return []
        frames = []
        for path in sorted(folder.iterdir()):
            if not path.is_file() or not config.is_image(path.name):
                continue
            try:
                with Image.open(path) as image:
                    frames.append(image.convert("RGB").copy())
            except Exception as exc:
                self.log.warning("cannot read sample %s: %s", path.name, exc)
        return frames

    def probe(self) -> tuple[bool, str]:
        count = len(self._samples())
        if count:
            return True, f"replaying {count} sample image(s) every {self.interval:.0f}s"
        return True, f"generating synthetic frames every {self.interval:.0f}s"

    def run(self, stop_event: threading.Event, emit: Emit) -> None:
        samples = self._samples()
        self.note("running", available=True, device=SIM_MODEL)

        while not stop_event.is_set():
            view = VIEWS[self.shot % len(VIEWS)]
            base = samples[self.shot % len(samples)] if samples else _synthetic_frame(view)
            self.shot += 1

            emit(
                CapturedImage(
                    filename=f"SIM_{self.shot:04d}.JPG",
                    data=_encode(_annotate(base, self.shot, view)),
                    source=self.name,
                    captured_at=utcnow_iso(),
                    camera_make=SIM_MAKE,
                    camera_model=SIM_MODEL,
                    origin=f"simulator://{self.shot}",
                    extra={"view": view},
                )
            )
            self.count_capture()
            self.note(f"shot {self.shot} ({view})", available=True, device=SIM_MODEL)
            stop_event.wait(self.interval)

        self.note("stopped", available=False)
