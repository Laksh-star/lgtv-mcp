"""Pause, resume and inspect the scheduled briefing, shared by the `briefing` CLI and the MCP tools.

The pause lives in ~/.config/lgtv/briefing-state.json as the last date to skip ("paused_through").
The 07:30 launchd job still fires; `briefing run` reads the file and skips. Once that date has
passed the pause is over, so nothing has to clear it.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta

from ..tv import CONFIG_DIR
from .config import LOG_FILE, Config

STATE_FILE = CONFIG_DIR / "briefing-state.json"
SERVER_LABEL = "local.lgtv.briefing.server"
RUN_LABEL = "local.lgtv.briefing"
FOREVER = date.max
DAY_NAMES = "Mon Tue Wed Thu Fri Sat Sun".split()


def _read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _write(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    tmp.replace(STATE_FILE)


def paused_through(today: date) -> date | None:
    """The last date the briefing is skipped, or None if it isn't paused (or the pause has ended)."""
    raw = _read().get("paused_through")
    if not raw:
        return None
    until = FOREVER if raw == "forever" else date.fromisoformat(raw)
    return until if until >= today else None


def scheduled_runs(cfg: Config, after: datetime, count: int) -> list[datetime]:
    """The next `count` scheduled briefing times strictly after `after` (in the briefing time zone)."""
    h, m = cfg.hour_minute
    days = set(cfg.weekdays)
    out: list[datetime] = []
    d = after.date()
    while days and len(out) < count:
        t = datetime(d.year, d.month, d.day, h, m, tzinfo=cfg.tz)
        if d.isoweekday() in days and t > after:
            out.append(t)
        d += timedelta(days=1)
    return out


def next_briefing(cfg: Config, now: datetime) -> datetime | None:
    """The next scheduled briefing that will actually run (skipping paused days)."""
    until = paused_through(now.date())
    if until == FOREVER:
        return None
    after = now
    if until:
        after = max(now, datetime(until.year, until.month, until.day, 23, 59, tzinfo=cfg.tz))
    runs = scheduled_runs(cfg, after, 1)
    return runs[0] if runs else None


def _day(d: date | datetime) -> str:
    return f"{DAY_NAMES[d.weekday()]} {d.day} {d:%b}"


def describe_next(cfg: Config, now: datetime) -> str:
    nxt = next_briefing(cfg, now)
    if nxt is None:
        return "no briefing scheduled (paused until you resume it)" if paused_through(now.date()) else "no briefing days set"
    if nxt.date() == now.date():
        return f"today at {nxt:%H:%M}"
    if nxt.date() == now.date() + timedelta(days=1):
        return f"tomorrow ({_day(nxt)}) at {nxt:%H:%M}"
    return f"{_day(nxt)} at {nxt:%H:%M}"


def pause(cfg: Config, now: datetime, *, briefings: int = 1, until: date | None = None,
          indefinitely: bool = False) -> str:
    """Skip the next `briefings` scheduled briefings, or every one through `until`, or all of them."""
    if indefinitely:
        _write({"paused_through": "forever", "set_at": now.isoformat(timespec="seconds")})
        return "The morning briefing is paused until you resume it."
    if until is None:
        if briefings < 1:
            raise ValueError("Skip at least one briefing.")
        # Count from the end of any pause already in place, so "skip one more" extends it.
        current = paused_through(now.date())
        start = now if current is None or current == FOREVER else max(
            now, datetime(current.year, current.month, current.day, 23, 59, tzinfo=cfg.tz))
        runs = scheduled_runs(cfg, start, briefings)
        if not runs:
            raise ValueError("No briefing days are set in briefing.toml, so there's nothing to pause.")
        until = runs[-1].date()
    if until < now.date():
        raise ValueError(f"{until:%Y-%m-%d} is in the past.")
    _write({"paused_through": until.isoformat(), "set_at": now.isoformat(timespec="seconds")})
    skipped = [r for r in scheduled_runs(cfg, now, 400) if r.date() <= until]
    which = ", ".join(_day(r) for r in skipped[:5]) + (f" and {len(skipped) - 5} more" if len(skipped) > 5 else "")
    head = f"Skipping {which}." if skipped else f"Paused through {_day(until)} (no briefings fall in that time)."
    return f"{head} Next briefing: {describe_next(cfg, now)}."


def resume(cfg: Config, now: datetime) -> str:
    was = paused_through(now.date())
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    lead = "Resumed the morning briefing." if was else "The morning briefing wasn't paused."
    return f"{lead} Next briefing: {describe_next(cfg, now)}."


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def agent_state(label: str) -> str:
    r = launchctl("print", f"gui/{os.getuid()}/{label}")
    if r.returncode:
        return "not installed"
    return next((ln.split("=", 1)[1].strip() for ln in r.stdout.splitlines()
                 if ln.strip().startswith("state =")), "loaded")


def restart_server() -> str:
    r = launchctl("kickstart", "-k", f"gui/{os.getuid()}/{SERVER_LABEL}")
    if r.returncode:
        raise RuntimeError("The page server isn't installed; run `uv run briefing install`."
                           if "Could not find service" in r.stderr else f"launchctl failed: {r.stderr.strip()}")
    return "Restarted the briefing page server."


def last_run(lines: int = 400) -> str | None:
    """The most recent outcome line of a briefing run, from the log."""
    if not LOG_FILE.exists():
        return None
    keep = ("briefing page is on the TV", "showed a notification instead", "showed the briefing as notifications",
            "couldn't show", "TV problem", "unexpected error", "skipping", "auto-off:")
    tail = LOG_FILE.read_text(errors="replace").splitlines()[-lines:]
    hits = [ln for ln in tail if any(k in ln for k in keep)]
    return hits[-1] if hits else None


def status(cfg: Config, now: datetime) -> dict:
    days = ", ".join(DAY_NAMES[d - 1] for d in sorted(set(cfg.weekdays)))
    until = paused_through(now.date())
    return {
        "schedule": f"{days} at {cfg.time}, TV off after {cfg.auto_off_minutes} min if the briefing is still showing",
        "paused": None if until is None else "until you resume it" if until == FOREVER else f"through {_day(until)}",
        "next_briefing": describe_next(cfg, now),
        "scheduler": {"running": "installed, a briefing is in progress", "not running": "installed, waiting"}.get(
            s := agent_state(RUN_LABEL), s),
        "page_server": agent_state(SERVER_LABEL),
        "last_run": last_run(),
    }
