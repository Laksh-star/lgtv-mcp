"""Data sources for the briefing: Google Calendar (iCal), Open-Meteo weather, RSS headlines.

Each source fails on its own: a failure is recorded in Briefing.errors and logged, and the
rest of the briefing still renders.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import aiohttp
import feedparser
import icalendar
import recurring_ical_events
import x_wr_timezone

from .config import Config, Feed, calendar_url

log = logging.getLogger("briefing")
UA = "lgtv-briefing/0.1 (+home use)"
TIMEOUT = aiohttp.ClientTimeout(total=15)

WMO = {
    0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


@dataclass
class Event:
    title: str
    start: datetime | None  # None for all-day events
    end: datetime | None
    all_day: bool
    location: str = ""


@dataclass
class Weather:
    now_c: float
    feels_c: float
    condition: str
    high_c: float
    low_c: float
    rain_pct: int | None
    sunrise: str
    sunset: str


@dataclass
class Headline:
    title: str
    source: str


@dataclass
class Briefing:
    generated: datetime
    city: str
    events: list[Event] | None = None
    weather: Weather | None = None
    headlines: dict[str, list[Headline]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        """One line for a toast, e.g. 'Morning briefing: 3 meetings today, first at 10:00 · 29°C, rain 0%'."""
        parts = []
        if self.events is not None:
            timed = [e for e in self.events if not e.all_day and e.start and e.start >= self.generated - timedelta(minutes=15)]
            n = len(self.events)
            if n == 0:
                parts.append("no meetings today")
            else:
                s = f"{n} meeting{'s' if n != 1 else ''} today"
                if timed:
                    earlier = any(not e.all_day and e.start and e.start < timed[0].start for e in self.events)
                    s += f", {'next' if earlier else 'first'} at {timed[0].start:%H:%M}"
                parts.append(s)
        if self.weather:
            w = self.weather
            rain = f", rain {w.rain_pct}%" if w.rain_pct is not None else ""
            parts.append(f"{w.now_c:.0f}°C{rain}")
        return "Morning briefing: " + (" · ".join(parts) if parts else "data unavailable right now")


# ---------------------------------------------------------------- calendar


def _declined(ev, owner: str) -> bool:
    if not owner:
        return False
    attendees = ev.get("ATTENDEE")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    for a in attendees:
        if str(a).lower().removeprefix("mailto:") == owner and str(a.params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


def parse_calendar(ics: bytes | str, day: date, tz) -> list[Event]:
    cal = x_wr_timezone.to_standard(icalendar.Calendar.from_ical(ics))
    owner = str(cal.get("X-WR-CALNAME", "")).lower()
    owner = owner if "@" in owner else ""
    start = datetime.combine(day, time.min, tz)
    end = start + timedelta(days=1)
    out: list[Event] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED" or _declined(ev, owner):
            continue
        s = ev.decoded("DTSTART")
        e = ev.decoded("DTEND") if ev.get("DTEND") else None
        all_day = isinstance(s, date) and not isinstance(s, datetime)
        if not all_day:
            s = s.replace(tzinfo=tz) if s.tzinfo is None else s.astimezone(tz)
            if isinstance(e, datetime):
                e = e.replace(tzinfo=tz) if e.tzinfo is None else e.astimezone(tz)
        out.append(Event(
            title=str(ev.get("SUMMARY", "")).strip() or "(No title)",
            start=None if all_day else s,
            end=None if all_day or not isinstance(e, datetime) else e,
            all_day=all_day,
            location=str(ev.get("LOCATION", "")).strip(),
        ))
    out.sort(key=lambda x: (not x.all_day, x.start or datetime.min.replace(tzinfo=tz)))
    return out


async def fetch_calendar(session: aiohttp.ClientSession, cfg: Config, now: datetime) -> list[Event]:
    url = await asyncio.to_thread(calendar_url, cfg)
    if not url:
        raise RuntimeError(f"no calendar address in the Keychain (service '{cfg.keychain_service}') — see README")
    if not url.startswith("https://"):
        raise RuntimeError("the Keychain item doesn't hold a web address; it should start with https://")
    async with session.get(url) as r:
        if r.status in (401, 403, 404):
            raise RuntimeError(f"Google rejected the calendar address (HTTP {r.status}); it may have been reset")
        r.raise_for_status()
        body = await r.read()
    return await asyncio.to_thread(parse_calendar, body, now.date(), cfg.tz)


# ---------------------------------------------------------------- weather


async def fetch_weather(session: aiohttp.ClientSession, cfg: Config) -> Weather:
    params = {
        "latitude": cfg.latitude, "longitude": cfg.longitude, "timezone": cfg.timezone, "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
    }
    async with session.get("https://api.open-meteo.com/v1/forecast", params=params) as r:
        r.raise_for_status()
        d: dict[str, Any] = await r.json()
    cur, day = d["current"], d["daily"]
    rain = day.get("precipitation_probability_max", [None])[0]
    return Weather(
        now_c=cur["temperature_2m"], feels_c=cur["apparent_temperature"],
        condition=WMO.get(cur.get("weather_code"), "—"),
        high_c=day["temperature_2m_max"][0], low_c=day["temperature_2m_min"][0],
        rain_pct=int(rain) if rain is not None else None,
        sunrise=day["sunrise"][0][-5:], sunset=day["sunset"][0][-5:],
    )


# ---------------------------------------------------------------- headlines


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


async def fetch_feed(session: aiohttp.ClientSession, feed: Feed) -> list[Headline]:
    async with session.get(feed.url) as r:
        r.raise_for_status()
        body = await r.read()
    parsed = await asyncio.to_thread(feedparser.parse, body)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"couldn't read the {feed.name} feed ({parsed.bozo_exception})")
    src = _clean(parsed.feed.get("title", feed.name))
    items = [Headline(_clean(e.get("title", "")), src) for e in parsed.entries]
    return [h for h in items if h.title][: feed.count]


# ---------------------------------------------------------------- all together


def _reason(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError | TimeoutError):
        return "timed out"
    if isinstance(e, aiohttp.ClientResponseError):
        return f"HTTP {e.status}"
    if isinstance(e, aiohttp.ClientError):
        return f"network error ({e.__class__.__name__})"
    return str(e) or e.__class__.__name__


async def gather(cfg: Config) -> Briefing:
    now = datetime.now(cfg.tz)
    b = Briefing(generated=now, city=cfg.city)
    async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as s:
        jobs = {"calendar": fetch_calendar(s, cfg, now), "weather": fetch_weather(s, cfg)}
        jobs.update({f"feed:{f.name}": fetch_feed(s, f) for f in cfg.feeds})
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    for name, res in zip(jobs, results):
        if isinstance(res, BaseException):
            b.errors[name] = _reason(res)
            log.error("%s failed: %s", name.replace("feed:", "headlines "), b.errors[name])
            continue
        if name == "calendar":
            b.events = res
        elif name == "weather":
            b.weather = res
        else:
            b.headlines[name.removeprefix("feed:")] = res
    return b
