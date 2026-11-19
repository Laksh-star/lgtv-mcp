"""Put the feed wall on the TV and take it off again.

Reuses the briefing's pieces: the busy check, the Mac's LAN address, the webOS browser launch and
the "did the TV actually load the page" check. The personal profile never takes over a busy TV
(a toast instead); the organization profile takes over by default (it's a dedicated screen).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from aiowebostv import endpoints as ep
from aiowebostv.exceptions import WebOsTvCommandError

from ..briefing.config import load_config as load_briefing_config
from ..briefing.show import BROWSER, PAGE_LOAD_TIMEOUT, WAKE_TIMEOUT, _busy, _local, mac_lan_ip
from .. import screensaver
from ..tv import TV, TVError, TVUnavailable
from . import schedule
from .config import Config, Profile, load_config
from .store import STATE_FILE, locked_json, read_state
from .wall import Wall, build, refresh

log = logging.getLogger("feedwall")
HOME_APP = "com.webos.app.home"


async def _wall_up(client, tv_host: str | None, bcfg) -> bool:
    """Is a wall already on screen (so showing another isn't taking anything over)? Right after a page-server
    restart the server hasn't heard from the page yet, so the saved state counts too."""
    if await wall_on_screen(client, tv_host, bcfg):
        return True
    return bool(read_state().get("showing")) and client.tv_state.current_app_id == BROWSER


def wall_url(tv_host: str | None, port: int, profile: str) -> str:
    return f"http://{mac_lan_ip(tv_host)}:{port}/wall?profile={profile}"


async def revive_wall(tv: TV, showing: dict[str, Any]) -> str | None:
    """Undo the TV's screensaver over the wall (see lgtv_mcp.screensaver for why it can't be prevented)."""
    client = await tv.connect()
    if not await screensaver.over_browser(client):
        return None
    bcfg = load_briefing_config()
    if await wall_on_screen(client, tv.host, bcfg) != showing.get("profile"):
        return None  # the browser is showing something else: leave it alone
    await screensaver.dismiss_and_reload(client, wall_url(tv.host, bcfg.port, showing["profile"]))
    return f"dismissed the TV's screensaver and reloaded the {showing['profile']} wall"


ON_SCREEN_SECONDS = 150  # the wall polls every 60 s; a view this recent means it's still on screen


async def wall_views(bcfg) -> dict[str, Any]:
    """{tv ip: {"at": unix time, "profile": name}} from the page server."""
    return await _local(bcfg, "GET", "/api/wall/views") or {}


async def wall_on_screen(client, tv_host: str, bcfg, since: float | None = None) -> str | None:
    """The profile the TV is showing, if the wall is on screen (browser in front and a recent view)."""
    if client is None or client.tv_state.current_app_id != BROWSER:
        return None
    v = (await wall_views(bcfg)).get(tv_host) or {}
    cutoff = since if since is not None else time.time() - ON_SCREEN_SECONDS
    return v.get("profile") if v.get("at", 0) >= cutoff else None


def _toast_text(wall: Wall) -> str:
    n = len(wall.shown)
    if not n:
        return "Feed wall: nothing new right now"
    p = wall.shown[0]
    who = p.author_handle if p.platform in ("bluesky", "mastodon", "x") else p.author_name
    return f"Feed wall: {n} post{'s' if n != 1 else ''} ready. Latest, {who}: {p.text}"[:200]


def _save_showing(entry: dict[str, Any] | None) -> None:
    with locked_json(STATE_FILE) as st:
        if entry is None:
            st.pop("showing", None)
        else:
            st["showing"] = entry


async def show_wall(tv: TV, profile_name: str, *, minutes: int | None = None, take_over: bool | None = None,
                    window: tuple[datetime, datetime] | None = None, cfg: Config | None = None) -> str:
    cfg = cfg or load_config()
    profile: Profile = cfg.profile(profile_name)
    if not cfg.sources_for(profile):
        raise ValueError(f"The {profile.name} profile has no sources yet; add some with manage_feed_source or in feedwall.toml.")
    bcfg = load_briefing_config()
    take_over = profile.take_over if take_over is None else take_over
    minutes = profile.minutes if minutes is None else minutes
    now = datetime.now(bcfg.tz)
    ends = schedule.end_time(now, minutes, window or schedule.current_window(profile.days, profile.hours, now))

    await refresh(cfg, profile)
    wall = build(cfg, profile)
    log.info("show %s: %d posts ready, %d pending, %d dropped", profile.name, len(wall.shown), len(wall.pending),
             sum(wall.dropped.values()))

    client, turned_on = None, False
    if await tv.reachable():
        try:
            client = await tv.connect()
        except TVUnavailable:
            client = None
    if client is None or not client.tv_state.is_on:
        log.info("show %s: TV is off, turning it on", profile.name)
        log.info("show %s: %s", profile.name, await tv.ensure_on(timeout=WAKE_TIMEOUT))
        client = await tv.connect()
        turned_on = True
        await asyncio.sleep(4)
    elif not take_over and not await _wall_up(client, tv.host, bcfg) and (busy := _busy(client)):
        msg = _toast_text(wall)
        await client.send_message(msg)
        log.info("show %s: TV busy (%s), showed a toast instead", profile.name, busy)
        return f"The TV was busy ({busy}), so it got a notification instead: {msg}"

    if await _local(bcfg, "GET", "/healthz") is None:
        raise TVError(f"The page server isn't running on port {bcfg.port}; restart it with restart_briefing_server "
                      "(or `uv run briefing install`).")
    url = wall_url(tv.host, bcfg.port, profile.name)
    started = time.time()
    try:
        await client.request(ep.LAUNCH, {"id": BROWSER, "params": {"target": url}})
    except WebOsTvCommandError as e:
        raise TVError(f"The TV refused to open its web browser ({e}).") from e
    shown = False
    deadline = time.monotonic() + PAGE_LOAD_TIMEOUT
    while time.monotonic() < deadline and not shown:
        await asyncio.sleep(1)
        shown = await wall_on_screen(client, tv.host, bcfg, since=started) == profile.name
    if not shown:
        log.error("show %s: the TV browser never loaded %s within %d s", profile.name, url, PAGE_LOAD_TIMEOUT)
        await client.send_message(_toast_text(wall))
        return "The TV's browser didn't load the wall, so it showed a notification instead. Try restart_briefing_server."

    prev = read_state().get("showing") or {}
    _save_showing({"profile": profile.name, "started": now.isoformat(timespec="seconds"),
                   "ends_at": ends.isoformat(timespec="seconds") if ends else None, "at_end": profile.at_end,
                   "turned_on": turned_on or bool(prev.get("turned_on"))})
    log.info("show %s: wall is on the TV (%s), until %s", profile.name, url, ends or "stopped")
    until = f"until {ends:%H:%M}" if ends else "until you stop it"
    pend = f", {len(wall.pending)} waiting for approval" if profile.approval and wall.pending else ""
    return (f"The {profile.name} feed wall is on the TV {until}: {len(wall.shown)} posts from "
            f"{len(wall.sources)} sources{pend}.")


async def end_wall(tv: TV, why: str, *, action: str | None = None) -> str:
    """Take the wall off the TV (Home, or power off for a dedicated screen) if it's still showing."""
    st = read_state().get("showing") or {}
    action = action or st.get("at_end") or "home"
    _save_showing(None)
    bcfg = load_briefing_config()
    try:
        client = await tv.connect()
    except TVError:
        log.info("end: %s; the TV is off or unreachable, nothing to do", why)
        return "The feed wall is stopped (the TV was already off)."
    if not client.tv_state.is_on:
        return "The feed wall is stopped (the TV was already off)."
    on = await wall_on_screen(client, tv.host, bcfg)
    if not on:
        log.info("end: %s; the wall isn't on screen (%s), left the TV alone", why, client.tv_state.current_app_id)
        return "The feed wall is stopped. It wasn't on screen, so the TV was left as it is."
    if action == "off":
        await client.power_off()
        log.info("end: %s; turned the TV off", why)
        return "Stopped the feed wall and turned the TV off."
    await client.request(ep.LAUNCH, {"id": HOME_APP})
    try:
        await client.close_app(BROWSER)
    except WebOsTvCommandError:
        pass
    log.info("end: %s; went back to the home screen", why)
    return "Stopped the feed wall; the TV is back on its home screen."


async def run_scheduled(tv: TV, profile_name: str) -> str:
    """The launchd job at the start of a profile's show hours."""
    cfg = load_config()
    profile = cfg.profile(profile_name)
    now = datetime.now(load_briefing_config().tz)
    if until := schedule.paused_through(read_state(), profile.name, now.date()):
        msg = f"{profile.name} wall paused {'until resumed' if until == schedule.FOREVER else f'through {until}'}, skipping"
        log.info("run: %s", msg)
        return msg
    window = schedule.current_window(profile.days, profile.hours, now)
    if window is None:
        log.info("run: %s: outside its show hours (%s), skipping", profile.name, profile.hours or "none set")
        return "outside show hours"
    return await show_wall(tv, profile.name, minutes=0, window=window, cfg=cfg)
