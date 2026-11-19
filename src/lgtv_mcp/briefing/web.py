"""Small LAN web server for the briefing page (runs as a launchd agent).

Only localhost, the paired TV and any `allow_ips` from briefing.toml may open the page,
because it shows your calendar.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiohttp import web

from .. import screensaver
from ..tv import load_config as load_tv_config
from .config import load_config
from .page import REFRESH_SECONDS, render
from .sources import Briefing, gather

log = logging.getLogger("briefing")
CACHE_SECONDS = 240

_cache: dict[str, tuple[float, Briefing]] = {}
_lock = asyncio.Lock()
last_view: dict[str, float] = {}  # ip -> unix time of last page load
view_since: dict[str, float] = {}  # ip -> when its current run of page loads (one every 5 min) began


async def current_briefing() -> Briefing:
    async with _lock:
        hit = _cache.get("b")
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        b = await gather(load_config())
        _cache["b"] = (time.monotonic(), b)
        return b


def _allowed(ip: str | None) -> bool:
    if ip in ("127.0.0.1", "::1"):
        return True
    tv_host = load_tv_config().get("host")
    return ip == tv_host or ip in load_config().allow_ips


@web.middleware
async def guard(request: web.Request, handler):
    ip = request.remote
    if not _allowed(ip):
        log.warning("server: refused page request from %s (not the TV; add it to allow_ips to permit)", ip)
        raise web.HTTPForbidden(text="This page is only available to the TV.")
    return await handler(request)


async def page(request: web.Request) -> web.Response:
    b = await current_briefing()
    now = time.time()
    if now - last_view.get(request.remote, 0) > REFRESH_SECONDS + 90:
        view_since[request.remote] = now
    last_view[request.remote] = now
    if request.remote not in ("127.0.0.1", "::1"):
        log.info("server: page shown to %s", request.remote)
    return web.Response(text=render(b, load_config().timezone), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def views(request: web.Request) -> web.Response:
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    return web.json_response(last_view)


async def refresh(request: web.Request) -> web.Response:
    """Drop the cache so the next page load fetches fresh data (used before each showing)."""
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    _cache.clear()
    return web.json_response({"ok": True})


def _wall_viewed_at(ip: str) -> float:
    try:
        from ..feedwall.web import wall_views
    except Exception:  # noqa: BLE001 - no feed wall, nothing to compare with
        return 0.0
    return (wall_views.get(ip) or {}).get("at", 0.0)


async def _watch_screensaver(app: web.Application) -> None:
    """While the briefing is on the TV (until its auto-off time), undo the TV's screensaver over it."""
    from ..tv import TV, TVError
    from .show import page_url

    tv, retry_at = None, 0.0
    while True:
        await asyncio.sleep(screensaver.CHECK_SECONDS)
        try:
            host, cfg = load_tv_config().get("host"), load_config()
            on = bool(host) and screensaver.briefing_on_screen(
                time.time(), last_view.get(host, 0.0), view_since.get(host, 0.0), _wall_viewed_at(host),
                REFRESH_SECONDS, cfg.auto_off_minutes or 30)
            if not on:
                if tv is not None:
                    await tv.close()
                    tv = None
                continue
            if time.monotonic() < retry_at:
                continue
            tv = tv or TV()
            try:
                client = await tv.connect()
                if await screensaver.over_browser(client):
                    await screensaver.dismiss_and_reload(client, page_url(tv, cfg))
                    log.info("server: dismissed the TV's screensaver and reloaded the briefing")
            except TVError:  # TV off or unreachable: don't search the network every few seconds
                retry_at = time.monotonic() + 60
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the watcher must keep running
            log.exception("server: screensaver watcher error: %s", e)


async def _start_watch(app: web.Application) -> None:
    app["briefing_saver_watch"] = asyncio.create_task(_watch_screensaver(app))


async def _stop_watch(app: web.Application) -> None:
    task = app.get("briefing_saver_watch")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/", page), web.get("/healthz", health),
                    web.get("/api/views", views), web.post("/api/refresh", refresh)])
    app.on_startup.append(_start_watch)
    app.on_cleanup.append(_stop_watch)
    try:  # the feed wall shares this server; a problem there must not take the briefing down
        from ..feedwall.web import setup as setup_feed_wall
        setup_feed_wall(app)
    except Exception as e:  # noqa: BLE001
        log.exception("server: feed wall routes not loaded: %s", e)
    return app


def serve() -> None:
    cfg = load_config()
    log.info("server: starting on port %d", cfg.port)
    web.run_app(make_app(), host="0.0.0.0", port=cfg.port, print=None, access_log=None)
