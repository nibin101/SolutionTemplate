"""Bridge test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRIDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_DIR))

from bridge.spool import Spool  # noqa: E402


@pytest.fixture()
def spool(tmp_path) -> Spool:
    return Spool(directory=tmp_path, max_attempts=3)
