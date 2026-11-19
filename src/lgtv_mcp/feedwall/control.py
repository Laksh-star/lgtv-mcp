"""Status, approvals, pauses and sources, shared by the `feedwall` CLI and the MCP tools."""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from ..briefing.control import agent_state
from ..briefing.control import SERVER_LABEL as PAGE_SERVER_LABEL
from . import schedule
from .config import CONFIG_FILE, KC_BLUESKY, KC_YOUTUBE, Config, keychain_get, load_config
from .store import STATE_FILE, locked_json, read_state
from .wall import build, decide, pending_list, refresh

AGENT_PREFIX = "local.lgtv.feedwall."


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    return "just now" if s < 90 else f"{s / 60:.0f} min ago" if s < 5400 else f"{s / 3600:.1f} h ago" if s < 172800 else f"{s / 86400:.0f} days ago"


def profile_status(cfg: Config, name: str, now: datetime, state: dict) -> dict[str, Any]:
    p = cfg.profile(name)
    wall = build(cfg, p)
    nxt = schedule.next_run(p.days, p.hours, now, state, name) if p.hours else None
    until = schedule.paused_through(state, name, now.date())
    days = ", ".join(schedule.DAY_NAMES[d - 1] for d in sorted(set(p.days)))
    return {
        "mode": p.mode,
        "moderation": ", ".join(x for x in (
            "approval queue" if p.approval else "", "allowlist required for open sources" if p.require_allowlist else
            ("allowlist" if p.allow_accounts else ""), "no reposts" if not p.include_reposts else "",
            f"{len(p.block_words) + len(p.block_patterns)} blocklist entries" if p.block_words or p.block_patterns else "") if x)
        or "always-on rules only",
        "posts_on_wall": len(wall.shown),
        "waiting_for_approval": len(wall.pending) if p.approval else None,
        "dropped_by_moderation": wall.dropped,
        "sources": [{"id": s["id"], "kind": s["kind"], "posts": s["posts"],
                     "state": "ok" if s["ok"] else "not fetched yet" if not s["last_updated"] and s["error"] == "not fetched yet"
                     else f"failing: {s['error']}",
                     "last_updated": _ago(s["last_updated"])} for s in wall.sources],
        "schedule": f"{days} {p.hours}, then {p.at_end}" if p.hours else "none (only when started from chat)",
        "agent": agent_state(AGENT_PREFIX + name) if p.hours else None,
        "paused": None if until is None else "until resumed" if until == schedule.FOREVER else f"through {until}",
        "next_showing": schedule.describe(nxt[0], now) if nxt else None,
    }


def status(cfg: Config, now: datetime) -> dict[str, Any]:
    state = read_state()
    showing = state.get("showing")
    return {
        "showing": None if not showing else {
            "profile": showing["profile"], "since": showing.get("started"),
            "ends": showing.get("ends_at") or "when stopped", "at_end": showing.get("at_end")},
        "page_server": agent_state(PAGE_SERVER_LABEL),
        "credentials": {"bluesky_app_password": bool(keychain_get(KC_BLUESKY)),
                        "youtube_api_key": bool(keychain_get(KC_YOUTUBE))},
        "settings": str(CONFIG_FILE) + ("" if CONFIG_FILE.exists() else " (not created yet, using defaults)"),
        "profiles": {name: profile_status(cfg, name, now, state) for name in cfg.profiles},
    }


async def pending(profile: str, limit: int = 20) -> list[dict[str, Any]]:
    cfg = load_config()
    p = cfg.profile(profile)
    await refresh(cfg, p)
    return pending_list(build(cfg, p), limit)


async def moderate_posts(profile: str, status_: str, ids: list[str] | None, newest: int | None) -> str:
    cfg = load_config()
    p = cfg.profile(profile)
    if not p.approval and status_ == "approved":
        return f"The {profile} profile doesn't use the approval queue, so its posts show without approval."
    await refresh(cfg, p)
    done, unknown = decide(build(cfg, p), status_, ids, newest)
    word = "Approved" if status_ == "approved" else "Rejected"
    msg = f"{word} {len(done)} post{'s' if len(done) != 1 else ''}" + (f" ({', '.join(done)})" if done else "") + "."
    if unknown:
        msg += f" Not found among pending posts: {', '.join(unknown)}."
    if done:
        msg += " The wall picks this up within a minute."
    return msg


def pause(profile: str, now: datetime, until: date | None, indefinitely: bool) -> str:
    cfg = load_config()
    p = cfg.profile(profile)
    if not p.hours:
        return f"The {profile} profile has no show hours, so there's no schedule to pause."
    if not indefinitely and until is None:
        raise ValueError("Give a date to pause through, or pause indefinitely.")
    if until and until < now.date():
        raise ValueError(f"{until} is in the past.")
    with locked_json(STATE_FILE) as st:
        st.setdefault("paused", {})[profile] = "forever" if indefinitely else until.isoformat()
    nxt = schedule.next_run(p.days, p.hours, now, read_state(), profile)
    head = f"The {profile} wall's schedule is paused " + ("until you resume it." if indefinitely else f"through {until:%a %d %b}.")
    return head + (f" Next showing: {schedule.describe(nxt[0], now)}." if nxt else "")


def resume(profile: str, now: datetime) -> str:
    cfg = load_config()
    p = cfg.profile(profile)
    with locked_json(STATE_FILE) as st:
        was = (st.get("paused") or {}).pop(profile, None)
    nxt = schedule.next_run(p.days, p.hours, now, read_state(), profile) if p.hours else None
    lead = f"Resumed the {profile} wall's schedule." if was else f"The {profile} wall wasn't paused."
    return lead + (f" Next showing: {schedule.describe(nxt[0], now)}." if nxt else "")


def list_sources(cfg: Config) -> list[dict[str, Any]]:
    out = []
    for s in cfg.sources.values():
        used = [n for n, p in cfg.profiles.items() if s in cfg.sources_for(p)]
        out.append({"id": s.id, "kind": s.kind, **{k: v for k, v in s.params.items()},
                    "profiles": used, "origin": s.origin, "enabled": s.id not in cfg.disabled})
    return out
