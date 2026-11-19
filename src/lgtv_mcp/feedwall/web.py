"""Feed wall routes on the briefing's page server (same port, same TV-only guard).

/wall?profile=…        the page
/wall/data?profile=…   posts for the page (refreshes stale sources first)
/wall/img/<key>        images, fetched and cached by the Mac so the TV only ever talks to it
/api/wall/views        localhost only: when the TV last polled the wall, and which profile

The server also watches the wall's end time, so "show it for 2 hours" ends on time even after
the chat that started it is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from .config import load_config
from .page import render_shell
from .sources import UA
from .store import IMAGE_MAX_BYTES, image_dir, image_url_for, prune_images, read_state
from .wall import build, page_data, refresh

log = logging.getLogger("feedwall")
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
WATCH_SECONDS = 20
SAVER_CHECK_SECONDS = 5  # how long the TV's screensaver can stay up over the wall

wall_views: dict[str, dict] = {}  # ip -> {"at": unix time, "profile": name}
_img_session: aiohttp.ClientSession | None = None
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
             "image/avif": ".avif"}
IMAGE_TYPES = {ext: ctype for ctype, ext in IMAGE_EXT.items()}


def _profile_name(request: web.Request) -> str:
    return request.query.get("profile", "personal")


async def page(request: web.Request) -> web.Response:
    name = _profile_name(request)
    try:
        load_config().profile(name)
    except ValueError as e:
        raise web.HTTPNotFound(text=str(e)) from e
    return web.Response(text=render_shell(name), content_type="text/html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})


async def data(request: web.Request) -> web.Response:
    name = _profile_name(request)
    try:
        cfg = load_config()
        profile = cfg.profile(name)
    except (ValueError, OSError) as e:
        log.error("server: wall data for %s failed: %s", name, e)
        return web.json_response({"error": str(e)}, status=500)
    await refresh(cfg, profile)
    wall = await asyncio.to_thread(build, cfg, profile)
    body = await asyncio.to_thread(page_data, cfg, wall, read_state().get("showing"))
    if request.remote not in ("127.0.0.1", "::1"):
        first = request.remote not in wall_views
        wall_views[request.remote] = {"at": time.time(), "profile": name}
        if first:
            log.info("server: wall '%s' shown to %s", name, request.remote)
    return web.json_response(body, headers={"Cache-Control": "no-store"})


async def image(request: web.Request) -> web.StreamResponse:
    global _img_session
    key = request.match_info["key"]
    if not key.isalnum() or len(key) != 32:
        raise web.HTTPNotFound()
    d = image_dir()
    for p in d.glob(f"{key}.*"):
        if p.suffix in IMAGE_TYPES:
            return web.Response(body=await asyncio.to_thread(p.read_bytes), content_type=IMAGE_TYPES[p.suffix],
                                headers={"Cache-Control": "max-age=86400"})
    url = image_url_for(key)
    if not url:
        raise web.HTTPNotFound()
    if _img_session is None or _img_session.closed:
        _img_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": UA})
    try:
        async with _img_session.get(url) as r:
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if r.status != 200 or ctype not in IMAGE_EXT or (r.content_length or 0) > IMAGE_MAX_BYTES:
                raise web.HTTPNotFound()
            body = bytearray()  # content.read(n) returns only what has arrived so far, so read it all
            async for chunk in r.content.iter_chunked(65536):
                body += chunk
                if len(body) > IMAGE_MAX_BYTES:
                    raise web.HTTPNotFound()
    except (aiohttp.ClientError, TimeoutError) as e:
        log.warning("server: image %s failed: %s", url[:80], e)
        raise web.HTTPNotFound() from e
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{key}.part"
    tmp.write_bytes(body)
    tmp.replace(d / f"{key}{IMAGE_EXT[ctype]}")
    return web.Response(body=bytes(body), content_type=ctype, headers={"Cache-Control": "max-age=86400"})


async def views(request: web.Request) -> web.Response:
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    return web.json_response(wall_views)


async def _watch(app: web.Application) -> None:
    """While a wall is showing: end it at its end time, and undo the TV's screensaver within a few
    seconds. Prune old images once a day."""
    from ..tv import TV, TVError
    from .show import end_wall, revive_wall

    last_prune, tv, tv_retry_at = 0.0, None, 0.0
    while True:
        showing = read_state().get("showing")
        await asyncio.sleep(SAVER_CHECK_SECONDS if showing else WATCH_SECONDS)
        try:
            st = read_state().get("showing") or {}
            if not st:
                if tv is not None:
                    await tv.close()
                    tv = None
            elif time.monotonic() >= tv_retry_at:
                tv = tv or TV()
                ends = st.get("ends_at")
                try:
                    if ends and datetime.fromisoformat(ends) <= datetime.now(timezone.utc):
                        await end_wall(tv, f"{st.get('profile')} wall reached its end time")  # it logs what it did
                    elif msg := await revive_wall(tv, st):
                        log.info("server: %s", msg)
                except TVError:  # TV off or unreachable: don't search the network every few seconds
                    tv_retry_at = time.monotonic() + 60
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                if n := await asyncio.to_thread(prune_images):
                    log.info("server: pruned %d old images", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the watcher must keep running
            log.exception("server: wall watcher error: %s", e)


async def _start(app: web.Application) -> None:
    app["feedwall_watch"] = asyncio.create_task(_watch(app))


async def _stop(app: web.Application) -> None:
    task = app.get("feedwall_watch")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if _img_session is not None:
        await _img_session.close()


def setup(app: web.Application) -> None:
    from .config import setup_logging
    setup_logging()
    app.add_routes([web.get("/wall", page), web.get("/wall/data", data),
                    web.get("/wall/img/{key}", image), web.get("/api/wall/views", views)])
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
