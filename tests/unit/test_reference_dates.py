"""Dates of reference perimeters: explicit formats, and month-first files."""

from __future__ import annotations

import datetime as dt

from narsat.reference.perimeters import _parse_date


def test_an_explicit_month_first_format_is_not_read_day_first() -> None:
    # Without the format, 07/05/2023 would be read as 7 May.
    assert _parse_date("07/05/2023", "%m/%d/%Y") == dt.date(2023, 7, 5)
    assert _parse_date("07/24/2023", "%m/%d/%Y") == dt.date(2023, 7, 24)


def test_iso_dates_still_work_with_a_format() -> None:
    # pyogrio turns date-typed attributes into ISO strings whatever the file's text form.
    assert _parse_date("2023-07-24", "%m/%d/%Y") == dt.date(2023, 7, 24)
    assert _parse_date("2023-07-24T00:00:00", "%m/%d/%Y") == dt.date(2023, 7, 24)


def test_unreadable_dates_are_none() -> None:
    assert _parse_date("24/07/2023", "%m/%d/%Y") is None
    assert _parse_date(None, "%m/%d/%Y") is None
    assert _parse_date("", None) is None
