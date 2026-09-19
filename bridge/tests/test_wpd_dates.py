"""Date handling for MTP/PTP devices.

These cases come from a real device: a phone that does not implement
DATE_CREATED at all and reports DATE_MODIFIED as "2024/12/06:20:33:30.000".
Parsing that wrongly silently breaks the capture window, which is the difference
between charting today's shoot and charting the whole card.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bridge.sources.wpd_ptp import _wpd_date_to_iso
from bridge.window import SKIP, TAKE, CaptureWindow


def local_iso(*args: int) -> str:
    """The same local-clock -> UTC conversion the parser performs."""
    return (
        datetime(*args)
        .astimezone()
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2024/12/06:20:33:30.000", local_iso(2024, 12, 6, 20, 33, 30)),
        ("2024/12/06:20:33:30", local_iso(2024, 12, 6, 20, 33, 30)),
        ("20241206T203330", local_iso(2024, 12, 6, 20, 33, 30)),
        ("2024-12-06 20:33:30", local_iso(2024, 12, 6, 20, 33, 30)),
    ],
)
def test_device_date_strings_are_understood(raw, expected):
    assert _wpd_date_to_iso(raw) == expected


def test_ole_float_dates_are_understood():
    # 45632.0 days after 1899-12-30 is 2024-12-06.
    assert _wpd_date_to_iso(45632.0).startswith("2024-12-06")


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", 0, -5])
def test_unusable_dates_return_none(raw):
    assert _wpd_date_to_iso(raw) is None


def window(since_minutes_ago: float) -> CaptureWindow:
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes_ago)
    window = CaptureWindow(
        watching_since=datetime.now(timezone.utc) - timedelta(hours=2)
    )
    window.update({"since": since.replace(microsecond=0).isoformat(), "until": None})
    return window


def device_date(minutes_ago: float) -> str:
    """A timestamp in the string form the phone under test actually reports."""
    local = datetime.now() - timedelta(minutes=minutes_ago)
    return local.strftime("%Y/%m/%d:%H:%M:%S.000")


def test_a_device_date_inside_the_session_survives_the_round_trip():
    """Parsing and judging have to agree, or a real shot is silently dropped."""
    assert window(10).verdict(_wpd_date_to_iso(device_date(5))) == TAKE


def test_a_device_date_from_last_year_is_not_charted():
    old = datetime(2024, 12, 6, 20, 33, 30).strftime("%Y/%m/%d:%H:%M:%S.000")
    assert window(10).verdict(_wpd_date_to_iso(old)) == SKIP


def test_a_date_string_we_cannot_parse_leaves_the_photo_alone():
    """Fail closed: unparsed means unprovable, and unprovable means not charted."""
    assert _wpd_date_to_iso("06-12-2024 20:33") is None
    assert window(10).verdict(_wpd_date_to_iso("06-12-2024 20:33")) == SKIP
