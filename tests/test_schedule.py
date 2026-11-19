"""Show-hours windows, pauses and end times."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from lgtv_mcp.feedwall import schedule

TZ = ZoneInfo("Asia/Kolkata")
WEEKDAYS = [1, 2, 3, 4, 5]


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def test_parse_hours():
    assert schedule.parse_hours("09:00-18:00") == (time(9), time(18))
    assert schedule.parse_hours("20:00-24:00") == (time(20), time(0))
    assert schedule.parse_hours("") is None
    for bad in ("9-18", "09:00-09:00", "25:00-26:00", "09:60-10:00"):
        with pytest.raises(ValueError):
            schedule.parse_hours(bad)


def test_current_window_on_a_weekday():
    w = schedule.current_window(WEEKDAYS, "09:00-18:00", at(2026, 11, 18, 10))  # Wednesday
    assert w == (at(2026, 11, 18, 9), at(2026, 11, 18, 18))
    assert schedule.current_window(WEEKDAYS, "09:00-18:00", at(2026, 11, 18, 18)) is None  # end is exclusive
    assert schedule.current_window(WEEKDAYS, "09:00-18:00", at(2026, 11, 21, 10)) is None  # Saturday


def test_overnight_window_belongs_to_the_day_it_starts():
    fri_night = schedule.current_window([5], "20:00-01:00", at(2026, 11, 21, 0, 30))  # Sat 00:30
    assert fri_night == (at(2026, 11, 20, 20), at(2026, 11, 21, 1))
    assert schedule.current_window([5], "20:00-01:00", at(2026, 11, 22, 0, 30)) is None  # Sun 00:30


def test_next_windows_skip_non_days():
    nxt = schedule.next_windows(WEEKDAYS, "09:00-18:00", at(2026, 11, 20, 19), 2)  # Fri evening
    assert [w[0] for w in nxt] == [at(2026, 11, 23, 9), at(2026, 11, 24, 9)]  # Mon, Tue


def test_pause_skips_through_the_date_and_forever():
    st = {"paused": {"organization": "2026-11-24"}}
    nxt = schedule.next_run(WEEKDAYS, "09:00-18:00", at(2026, 11, 20, 19), st, "organization")
    assert nxt[0] == at(2026, 11, 25, 9)
    assert schedule.next_run(WEEKDAYS, "09:00-18:00", at(2026, 11, 20, 19), {"paused": {"organization": "forever"}},
                             "organization") is None
    assert schedule.paused_through(st, "organization", date(2026, 11, 25)) is None  # over by itself
    assert schedule.paused_through(st, "personal", date(2026, 11, 20)) is None  # per profile


def test_end_time_is_the_earlier_of_minutes_and_window():
    now = at(2026, 11, 18, 17)
    window = (at(2026, 11, 18, 9), at(2026, 11, 18, 18))
    assert schedule.end_time(now, 120, window) == at(2026, 11, 18, 18)
    assert schedule.end_time(now, 30, window) == at(2026, 11, 18, 17, 30)
    assert schedule.end_time(now, 0, window) == at(2026, 11, 18, 18)
    assert schedule.end_time(now, 0, None) is None


def test_describe():
    now = at(2026, 11, 18, 8)
    assert schedule.describe(at(2026, 11, 18, 9), now) == "today at 09:00"
    assert schedule.describe(at(2026, 11, 19, 9), now) == "tomorrow at 09:00"
    assert schedule.describe(at(2026, 11, 23, 9), now) == "Mon 23 Nov at 09:00"
