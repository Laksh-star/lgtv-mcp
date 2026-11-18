"""Put the briefing on the TV: decide whether to take over the screen, open the page in the
webOS browser (confirmed by the TV actually loading it), fall back to notifications, and turn
the TV off later if the briefing is still showing."""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import aiohttp
from aiowebostv import endpoints as ep
from aiowebostv.exceptions import WebOsTvCommandError

from ..tv import TV, TVError, TVUnavailable, port_open
from .config import Config
from .page import REFRESH_SECONDS
from .sources import Briefing, gather

log = logging.getLogger("briefing")

BROWSER = "com.webos.app.browser"
IDLE_APPS = {None, "", "com.webos.app.home"}
PAGE_LOAD_TIMEOUT = 45  # seconds for the TV browser to start and fetch the page
WAKE_TIMEOUT = 90       # seconds to keep trying to turn the TV on (Wake-on-LAN from full power-off can be slow)


def mac_lan_ip(tv_host: str) -> str:
    """The address of this Mac on the TV's network (the route the OS would use to reach it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((tv_host, 3001))
        return s.getsockname()[0]


def page_url(tv: TV, cfg: Config) -> str:
    return f"http://{mac_lan_ip(tv.host)}:{cfg.port}/"


async def _local(cfg: Config, method: str, path: str) -> dict | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            async with s.request(method, f"http://127.0.0.1:{cfg.port}{path}") as r:
                return await r.json() if r.status == 200 else None
    except (aiohttp.ClientError, TimeoutError):
        return None


async def _tv_viewed_since(cfg: Config, tv_ip: str, since: float) -> bool:
    views = await _local(cfg, "GET", "/api/views") or {}
    return views.get(tv_ip, 0) >= since


def _busy(client) -> str | None:
    """What the TV is doing, if it's something the briefing shouldn't interrupt."""
    s = client.tv_state
    app = s.current_app_id
    title = (s.apps.get(app) or s.inputs.get(app) or {}).get("title") or (s.inputs.get(app) or {}).get("label") or app
    if any(m.get("playState") == "playing" for m in s.media_state or []):
        return f"{title} is playing"
    if app in IDLE_APPS or (s.power_state or {}).get("state") == "Screen Saver":
        return None
    if app == "com.webos.app.livetv":
        return "Live TV is on"
    return f"{title} is open"


async def _toasts(client, b: Briefing) -> None:
    msgs = [b.summary()]
    if b.events:
        items = [("All day" if e.all_day else f"{e.start:%H:%M}") + f" {e.title}" for e in b.events[:3]]
        more = f" (+{len(b.events) - 3} more)" if len(b.events) > 3 else ""
        msgs.append("Today: " + "; ".join(items) + more)
    if b.weather:
        w = b.weather
        msgs.append(f"{b.city}: {w.now_c:.0f}°C {w.condition}, high {w.high_c:.0f}° low {w.low_c:.0f}°"
                    + (f", rain {w.rain_pct}%" if w.rain_pct is not None else ""))
    for name, items in b.headlines.items():
        if items:
            msgs.append(f"{name}: {items[0].title}")
    for i, m in enumerate(msgs):
        await client.send_message(m[:240])
        if i + 1 < len(msgs):
            await asyncio.sleep(7)


def _describe(b: Briefing) -> str:
    ev = "calendar failed" if b.events is None else f"{len(b.events)} events"
    wx = "weather failed" if b.weather is None else f"{b.weather.now_c:.0f}°C"
    news = ", ".join(f"{k} {len(v)}" for k, v in b.headlines.items()) or "no headlines"
    return f"{ev}, {wx}, headlines: {news}"


async def show(tv: TV, cfg: Config, *, take_over: bool, auto_off_minutes: int = 0,
               wake_timeout: float = WAKE_TIMEOUT) -> str:
    """Show the briefing. take_over=False leaves a busy TV alone and only shows a notification."""
    b = await gather(cfg)
    log.info("run: data ready (%s)", _describe(b))

    client = None
    if tv.host and await port_open(tv.host):
        try:
            client = await tv.connect()
        except TVUnavailable:
            client = None
    if client is None or not client.tv_state.is_on:
        log.info("run: TV is off, turning it on (trying for up to %.0f s)", wake_timeout)
        log.info("run: %s", await tv.ensure_on(timeout=wake_timeout))
        client = await tv.connect()
        await asyncio.sleep(4)  # let the home screen finish loading before opening the browser
    elif not take_over and (busy := _busy(client)):
        await client.send_message(b.summary())
        log.info("run: TV busy (%s), showed a notification instead: %s", busy, b.summary())
        return f"The TV was busy ({busy}), so it got a notification instead: {b.summary()}"

    shown = False
    if await _local(cfg, "GET", "/healthz") is None:
        log.error("run: the briefing page server isn't running on port %d (see `briefing status`); "
                  "falling back to notifications", cfg.port)
    else:
        await _local(cfg, "POST", "/api/refresh")
        url = page_url(tv, cfg)
        started = time.time()
        try:
            await client.request(ep.LAUNCH, {"id": BROWSER, "params": {"target": url}})
        except WebOsTvCommandError as e:
            log.error("run: the TV refused to open its web browser (%s); falling back to notifications", e)
        else:
            deadline = time.monotonic() + PAGE_LOAD_TIMEOUT
            while time.monotonic() < deadline and not shown:
                await asyncio.sleep(1)
                shown = await _tv_viewed_since(cfg, tv.host, started)
            if shown:
                log.info("run: briefing page is on the TV (%s, loaded after %.0f s)", url, time.time() - started)
            else:
                log.error("run: the TV browser opened but never loaded %s within %d s; "
                          "falling back to notifications", url, PAGE_LOAD_TIMEOUT)

    if not shown:
        try:
            await _toasts(client, b)
        except (TVError, WebOsTvCommandError) as e:
            log.error("run: couldn't show notifications either; the TV went off or dropped off the network (%s)", e)
            raise TVUnavailable(tv.unreachable_message()) from e
        log.info("run: showed the briefing as notifications")
        return "Couldn't show the briefing page, so the TV showed it as notifications instead."

    if auto_off_minutes > 0:
        await _auto_off(tv, cfg, auto_off_minutes)
    return f"The briefing is on the TV ({b.summary().removeprefix('Morning briefing: ')})."


async def _auto_off(tv: TV, cfg: Config, minutes: int) -> None:
    log.info("run: will turn the TV off in %d min if the briefing is still on screen", minutes)
    await asyncio.sleep(minutes * 60)
    try:
        client = await tv.connect()
    except TVError:
        log.info("auto-off: TV already off or unreachable, nothing to do")
        return
    s = client.tv_state
    recently_viewed = await _tv_viewed_since(cfg, tv.host, time.time() - REFRESH_SECONDS - 90)
    if not s.is_on:
        log.info("auto-off: TV is already off")
    elif s.current_app_id == BROWSER and recently_viewed:
        await client.power_off()
        log.info("auto-off: briefing still on screen after %d min, turned the TV off", minutes)
    else:
        log.info("auto-off: left the TV on, someone is using it (%s)", s.current_app_id)
