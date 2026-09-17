"""Rotating file + console logging.

A background service is invisible by definition, so the log is the only place a
failed transfer can surface. It rotates so a long clinic day cannot fill a disk.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import config

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger("bridge")
    if _configured:
        return logger

    config.log_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(getattr(logging, config.log_level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-24s %(message)s")

    file_handler = RotatingFileHandler(
        config.log_dir / "bridge.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # In tray mode the process is launched with pythonw.exe, which has no
    # console and leaves sys.stderr as None - guard before attaching.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    logger.propagate = False
    _configured = True
    return logger
