"""Show-hours windows and pauses for the feed wall. Pure functions on time-zone-aware datetimes.

A profile's window is `days` (1 = Monday) plus `hours` "HH:MM-HH:MM". A window may run past
midnight ("20:00-01:00"); it belongs to the day it starts. Pauses are per profile, kept in
feedwall-state.json as the last date to skip (or "forever"), like the briefing's.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

FOREVER = date.max
DAY_NAMES = "Mon Tue Wed Thu Fri Sat Sun".split()


def parse_hours(hours: str) -> tuple[time, time] | None:
    if not hours or not hours.strip():
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", hours)
    if not m:
        raise ValueError(f"hours '{hours}' should look like 09:00-18:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h2 == 24 and m2):
        raise ValueError(f"hours '{hours}' isn't a valid time range")
    start, end = time(h1, m1), time(0, 0) if h2 == 24 else time(h2, m2)
    if start == end:
        raise ValueError(f"hours '{hours}' starts and ends at the same time")
    return start, end


def _window_on(d: date, days: list[int], hours: tuple[time, time], tz) -> tuple[datetime, datetime] | None:
    if d.isoweekday() not in days:
        return None
    start = datetime.combine(d, hours[0], tz)
    end = datetime.combine(d if hours[1] > hours[0] else d + timedelta(days=1), hours[1], tz)
    return start, end


def current_window(days: list[int], hours: str, now: datetime) -> tuple[datetime, datetime] | None:
    """The window `now` falls in, or None."""
    h = parse_hours(hours)
    if h is None:
        return None
    for d in (now.date(), now.date() - timedelta(days=1)):
        w = _window_on(d, days, h, now.tzinfo)
        if w and w[0] <= now < w[1]:
            return w
    return None


def next_windows(days: list[int], hours: str, after: datetime, count: int = 1) -> list[tuple[datetime, datetime]]:
    """The next `count` windows starting strictly after `after`."""
    h = parse_hours(hours)
    out: list[tuple[datetime, datetime]] = []
    if h is None or not days:
        return out
    d = after.date()
    while len(out) < count and d <= after.date() + timedelta(days=400):
        w = _window_on(d, days, h, after.tzinfo)
        if w and w[0] > after:
            out.append(w)
        d += timedelta(days=1)
    return out


def paused_through(state: dict, profile: str, today: date) -> date | None:
    raw = (state.get("paused") or {}).get(profile)
    if not raw:
        return None
    until = FOREVER if raw == "forever" else date.fromisoformat(raw)
    return until if until >= today else None


def next_run(days: list[int], hours: str, now: datetime, state: dict, profile: str) -> tuple[datetime, datetime] | None:
    """The next window that will actually run (skipping paused days)."""
    until = paused_through(state, profile, now.date())
    if until == FOREVER:
        return None
    after = now
    if until:
        after = max(now, datetime.combine(until, time(23, 59), now.tzinfo))
    w = next_windows(days, hours, after, 1)
    return w[0] if w else None


def describe(dt: datetime, now: datetime) -> str:
    if dt.date() == now.date():
        return f"today at {dt:%H:%M}"
    if dt.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {dt:%H:%M}"
    return f"{DAY_NAMES[dt.weekday()]} {dt.day} {dt:%b} at {dt:%H:%M}"


def end_time(now: datetime, minutes: int | None, window: tuple[datetime, datetime] | None) -> datetime | None:
    """When a wall started now should end: after `minutes`, or at the end of the show-hours window,
    whichever is first. None = until stopped."""
    ends = [t for t in ((now + timedelta(minutes=minutes)) if minutes else None, window[1] if window else None) if t]
    return min(ends) if ends else None
