"""Small LAN web server for the briefing page (runs as a launchd agent).

Only localhost, the paired TV and any `allow_ips` from briefing.toml may open the page,
because it shows your calendar.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from ..tv import load_config as load_tv_config
from .config import load_config
from .page import render
from .sources import Briefing, gather

log = logging.getLogger("briefing")
CACHE_SECONDS = 240

_cache: dict[str, tuple[float, Briefing]] = {}
_lock = asyncio.Lock()
last_view: dict[str, float] = {}  # ip -> unix time of last page load


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
    last_view[request.remote] = time.time()
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


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/", page), web.get("/healthz", health),
                    web.get("/api/views", views), web.post("/api/refresh", refresh)])
    return app


def serve() -> None:
    cfg = load_config()
    log.info("server: starting on port %d", cfg.port)
    web.run_app(make_app(), host="0.0.0.0", port=cfg.port, print=None, access_log=None)
