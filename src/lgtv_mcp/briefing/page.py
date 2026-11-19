"""Render the briefing as a TV-readable HTML page (1080p, dark, large type, 5-minute refresh)."""

from __future__ import annotations

import time

from datetime import datetime
from html import escape

from .sources import Briefing, Event

REFRESH_SECONDS = 300
MAX_EVENTS = 5

CSS = """
:root {
  --bg: #0c1118; --panel: #141b25; --line: #233041;
  --fg: #eef2f7; --muted: #93a1b4; --dim: #5f6d80;
  --accent: #ffb547; --cool: #7cc4ff; --warn: #ff8a7a;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg); overflow: hidden;
  font-family: "LG Smart UI", "Roboto", "Helvetica Neue", Arial, sans-serif; }
/* 1920x1080 frame, padded for TV overscan */
.frame { height: 100vh; padding: 4.5vh 4vw 3.5vh; display: grid; gap: 2.6vh;
  grid-template-rows: auto 1fr auto auto; }
header { display: flex; justify-content: space-between; align-items: flex-end; gap: 3vw; }
.day { font-size: 4.8vw; font-weight: 700; line-height: 0.95; letter-spacing: -0.02em; }
.date { font-size: 2vw; color: var(--muted); margin-top: 0.8vh; }
.clock { font-size: 5.8vw; font-weight: 300; line-height: 0.9; font-variant-numeric: tabular-nums; color: var(--accent); }
.greet { font-size: 1.5vw; color: var(--muted); text-align: right; margin-top: 0.6vh; }

.main { display: grid; grid-template-columns: 34fr 66fr; gap: 2.4vw; min-height: 0; }
.panel { background: var(--panel); border-radius: 1.4vw; padding: 2.6vh 2.2vw; min-height: 0; overflow: hidden;
  display: flex; flex-direction: column; gap: 1.4vh; }
.label { font-size: 1.25vw; text-transform: uppercase; letter-spacing: 0.14em; color: var(--dim); font-weight: 600; }

.temp { font-size: 6.6vw; font-weight: 300; line-height: 0.9; letter-spacing: -0.03em; }
.cond { font-size: 2.2vw; }
.wrow { display: flex; gap: 1.8vw; flex-wrap: wrap; font-size: 1.8vw; color: var(--muted); }
.wrow b { color: var(--fg); font-weight: 600; }
.rain b { color: var(--cool); }
.feels { color: var(--muted); font-size: 1.8vw; }
.sun { font-size: 1.5vw; color: var(--dim); margin-top: auto; }

.events { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1.5vh; }
.ev { display: grid; grid-template-columns: 8vw 1fr; gap: 1.4vw; align-items: baseline; font-size: 2.1vw; }
.ev .t { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 2vw; }
.ev .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ev .loc { font-size: 1.5vw; color: var(--dim); margin-left: 1vw; }
.ev.past { opacity: 0.4; }
.ev.next .t, .ev.next .n { color: var(--accent); }
.ev.allday .t { color: var(--cool); font-size: 1.4vw; text-transform: uppercase; letter-spacing: 0.08em; }
.empty { font-size: 2.4vw; color: var(--muted); }
.more { font-size: 1.6vw; color: var(--dim); }

.news { display: grid; grid-template-columns: 1fr 1fr; gap: 2.4vw; }
.news ol { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 1vh; }
.news li { font-size: 1.5vw; line-height: 1.22; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; padding-left: 1.4vw; border-left: 0.25vw solid var(--line); }
.news .col { display: flex; flex-direction: column; gap: 1vh; min-width: 0; }

footer { display: flex; justify-content: space-between; gap: 2vw; font-size: 1.25vw; color: var(--dim); }
.err { color: var(--warn); }
"""

CLOCK_JS = """
(function () {
  var serverMs = %(server_ms)d, t0 = Date.now(), tz = "%(tz)s";
  var fmt;
  try { fmt = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }); }
  catch (e) { fmt = null; }
  function tick() {
    var d = new Date(serverMs + (Date.now() - t0));
    document.getElementById("clock").textContent = fmt ? fmt.format(d)
      : ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  tick(); setInterval(tick, 10000);
})();
"""


def _greeting(now: datetime) -> str:
    return "Good morning" if now.hour < 12 else ("Good afternoon" if now.hour < 17 else "Good evening")


def _event_rows(events: list[Event], now: datetime) -> str:
    timed = [e for e in events if not e.all_day]
    upcoming = next((e for e in timed if (e.end or e.start) > now), None)
    rows = []
    for e in events[:MAX_EVENTS]:
        loc = f'<span class="loc">{escape(e.location)}</span>' if e.location else ""
        if e.all_day:
            rows.append(f'<li class="ev allday"><span class="t">All day</span><span class="n">{escape(e.title)}{loc}</span></li>')
            continue
        cls = "ev"
        if e is upcoming:
            cls += " next"
        elif (e.end or e.start) <= now:
            cls += " past"
        rows.append(f'<li class="{cls}"><span class="t">{e.start:%H:%M}</span><span class="n">{escape(e.title)}{loc}</span></li>')
    more = len(events) - MAX_EVENTS
    extra = f'<div class="more">+{more} more</div>' if more > 0 else ""
    return f'<ul class="events">{"".join(rows)}</ul>{extra}'


def render(b: Briefing, tz_name: str) -> str:
    now = b.generated
    # --- weather
    if b.weather:
        w = b.weather
        rain = f'<span class="rain">Rain <b>{w.rain_pct}%</b></span>' if w.rain_pct is not None else ""
        weather = f"""
        <div class="label">{escape(b.city)} now</div>
        <div class="temp">{w.now_c:.0f}°</div>
        <div class="cond">{escape(w.condition)} <span class="feels">· feels {w.feels_c:.0f}°</span></div>
        <div class="wrow"><span>High <b>{w.high_c:.0f}°</b></span><span>Low <b>{w.low_c:.0f}°</b></span>{rain}</div>
        <div class="sun">Sunrise {w.sunrise} · Sunset {w.sunset}</div>"""
    else:
        weather = f'<div class="label">{escape(b.city)}</div><div class="empty">Weather unavailable</div>'
    # --- calendar
    if b.events is None:
        agenda = '<div class="empty">Calendar unavailable</div>'
    elif not b.events:
        agenda = '<div class="empty">Nothing on the calendar today</div>'
    else:
        agenda = _event_rows(b.events, now)
    count = f" · {len(b.events)}" if b.events else ""
    # --- headlines
    cols = []
    for name, items in b.headlines.items():
        lis = "".join(f"<li>{escape(h.title)}</li>" for h in items)
        cols.append(f'<div class="col"><div class="label">{escape(name)}</div><ol>{lis}</ol></div>')
    for key in b.errors:
        if key.startswith("feed:"):
            cols.append(f'<div class="col"><div class="label">{escape(key[5:])}</div><div class="empty">Headlines unavailable</div></div>')
    news = f'<section class="news">{"".join(cols)}</section>' if cols else ""
    # --- footer
    problems = []
    if "calendar" in b.errors:
        problems.append("calendar: " + b.errors["calendar"])
    if "weather" in b.errors:
        problems.append("weather: " + b.errors["weather"])
    problems += [f"{k[5:]} headlines: {v}" for k, v in b.errors.items() if k.startswith("feed:")]
    err = f'<span class="err">{escape(" · ".join(problems))}</span>' if problems else "<span></span>"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Morning briefing</title>
<style>{CSS}</style>
</head><body>
<div class="frame">
  <header>
    <div><div class="day">{now:%A}</div><div class="date">{now.day} {now:%B %Y}</div></div>
    <div><div class="clock" id="clock">{now:%H:%M}</div><div class="greet">{_greeting(now)}</div></div>
  </header>
  <div class="main">
    <section class="panel">{weather}</section>
    <section class="panel"><div class="label">Today{count}</div>{agenda}</section>
  </div>
  {news}
  <footer><span>Updated {now:%H:%M} · refreshes every 5 minutes</span>{err}</footer>
</div>
<script>{CLOCK_JS % {"server_ms": int(time.time() * 1000), "tz": tz_name}}</script>
</body></html>"""
