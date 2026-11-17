"""play_title: open a movie or episode in the right streaming app on the TV, deep-linked if possible."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

from aiowebostv import endpoints as ep
from aiowebostv.exceptions import WebOsTvCommandError

from . import titles

log = logging.getLogger("lgtv_mcp.play")


@dataclass(frozen=True)
class AppSpec:
    key: str
    name: str
    ids: tuple[str, ...]          # known webOS app ids
    title_words: tuple[str, ...]  # or match on the launcher title
    # content id -> extra launch payload; None = no working deep link (open the app + toast)
    deep_link: Callable[[str], dict[str, Any]] | None
    id_hint: str                  # where the id shows up, for a web search
    aliases: tuple[str, ...] = field(default=())
    # After a deep link: "playback" = the app starts the video itself (Netflix);
    # "details" = it stops on the title's page with Play focused (Prime Video), so press OK on Play.
    lands_on: str = "playback"


def _netflix(cid: str) -> dict[str, Any]:
    return {"contentId": f"m=https://www.netflix.com/watch/{cid}&source_type=4"}


def _prime(cid: str) -> dict[str, Any]:
    return {"contentId": cid}  # must be the GTI (amzn1.dv.gti.<uuid>); other Prime ids open Home


def _youtube(cid: str) -> dict[str, Any]:
    if cid.startswith("search:"):
        return {"params": {"contentTarget": f"https://www.youtube.com/tv?launch=search&vq={quote_plus(cid[7:])}"}}
    return {"params": {"contentTarget": f"https://www.youtube.com/tv?v={cid}"}}


# Order = preference when the caller doesn't name an app. Deep-link status is from live tests on
# the user's TV (see README "Deep links"); apps without one are opened with a toast instead.
APPS: dict[str, AppSpec] = {
    "netflix": AppSpec(
        "netflix", "Netflix", ("netflix",), ("netflix",), _netflix,
        "the number in netflix.com/title/<id> (movie or show) or netflix.com/watch/<id> (episode)",
    ),
    "prime": AppSpec(
        "prime", "Prime Video", ("amazon", "com.amazon.amazonvideo.webos"), ("prime video", "amazon"),
        _prime, "the amzn1.dv.gti.<uuid> id (seen in primevideo.com/detail/amzn1.dv.gti.... links); "
        "short detail ids like 0PKD430... open Prime's home page instead", ("amazon", "prime video", "amazon prime"),
        lands_on="details",
    ),
    "hotstar": AppSpec(
        "hotstar", "JioHotstar", ("com.hotstar.app", "hotstar"), ("hotstar",),
        None, "the trailing number in hotstar.com/in/movies/<slug>/<id> (not used: no deep link works)",
        ("jiohotstar", "disney+ hotstar"),
    ),
    "zee5": AppSpec(
        "zee5", "ZEE5", ("com.zee5.app", "zee5"), ("zee5",),
        None, "the id at the end of zee5.com/movies/details/<slug>/<id> (not used: deep links hang on a "
        "black screen)",
    ),
    "youtube": AppSpec(
        "youtube", "YouTube", ("youtube.leanback.v4",), ("youtube",), _youtube,
        "the v= value of a youtube.com/watch URL (only for films YouTube streams legitimately)",
    ),
}


def service_key(name: str) -> str | None:
    n = name.strip().lower()
    for key, spec in APPS.items():
        if n == key or n == spec.name.lower() or n in spec.aliases or n in spec.ids:
            return key
    for key, spec in APPS.items():
        if any(w in n for w in spec.title_words):
            return key
    return None


def installed_services(client) -> dict[str, str]:
    """service key -> installed webOS app id."""
    apps = {app_id: (a.get("title") or "").lower() for app_id, a in client.tv_state.apps.items()}
    found: dict[str, str] = {}
    for key, spec in APPS.items():
        for app_id in spec.ids:
            if app_id in apps:
                found[key] = app_id
                break
        else:
            for app_id, title in apps.items():
                if any(w in title for w in spec.title_words):
                    found[key] = app_id
                    break
    return found


async def _foreground(client) -> tuple[str | None, list[dict[str, Any]]]:
    try:
        info = await client.request(ep.GET_CURRENT_APP_INFO)
        app = info.get("appId")
    except WebOsTvCommandError:
        app = client.tv_state.current_app_id
    try:
        media = await client.get_media_foreground_app()
    except WebOsTvCommandError:
        media = []
    return app, media


def _playing(media: list[dict[str, Any]], app_id: str) -> str | None:
    states = [m.get("playState") for m in media if m.get("appId") == app_id]
    return next((s for s in states if s), None)


def _media_ids(media: list[dict[str, Any]], app_id: str, state: str = "playing") -> set[str]:
    return {m.get("mediaId") for m in media if m.get("appId") == app_id and m.get("playState") == state}


async def _wait_for_playback(client, app_id: str, timeout: float,
                             ignore: set[str] = frozenset()) -> tuple[float, str | None, str | None, set[str]]:
    """Poll until the app has a playing media session not in `ignore` (or timeout).

    Returns (seconds waited, foreground app, play state, playing media ids).
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        app, media = await _foreground(client)
        playing = _media_ids(media, app_id) - set(ignore)
        state = "playing" if playing else _playing(media, app_id)
        waited = loop.time() - start
        if playing or waited >= timeout:
            return round(waited, 1), app, state, _media_ids(media, app_id)
        await asyncio.sleep(1.5)


async def _press_ok(client, report: dict[str, Any], why: str) -> None:
    report.setdefault("ok_presses", []).append(why)
    await client.button("ENTER")


# A capture of a black screen (loading, or full-screen video, which the capture leaves out) is a
# ~9-15 KB JPEG; any app UI (profile picker, title page, home) is 20-140 KB. Measured on this TV.
BLANK_MAX_BYTES = 16_000


async def _ui_shown(client, name: str) -> tuple[bool, str | None]:
    path = await capture_screen(client, name)
    if not path:
        return True, None  # can't tell; assume there is something to press
    return Path(path).stat().st_size > BLANK_MAX_BYTES, path


async def _wait_for_ui(client, app_id: str, timeout: float) -> tuple[float, bool, set[str]]:
    """Poll until the app shows some UI (not a blank/loading screen). Returns (waited, shown, playing ids)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        _app, media = await _foreground(client)
        shown, _ = await _ui_shown(client, "probe")
        waited = loop.time() - start
        if shown or waited >= timeout:
            return round(waited, 1), shown, _media_ids(media, app_id)
        await asyncio.sleep(2)


async def _start_playback(client, app_id: str, lands_on: str, delay: float) -> dict[str, Any]:
    """After a deep link, get past a 'Who's watching?' screen and make sure the video starts.

    Learned on the TV (see field-notes/):
    - Netflix ("playback"): starts the film itself, behind an info overlay, ~6-20 s after launch.
      An OK press then would pause it, so OK is pressed only if nothing plays and UI is showing.
    - Prime Video ("details"): profile picker (OK picks the focused profile), then the title page with
      Play focused and a trailer running; OK on Play starts the film full-screen under a new media id.
    """
    report: dict[str, Any] = {}
    if lands_on == "playback":
        waited, app, state, ids = await _wait_for_playback(client, app_id, delay)
        report.update(waited_s=waited, foreground_app=app, play_state=state)
        if app != app_id:
            report["result"] = "app is not in the foreground; nothing pressed"
            return report
        if state != "playing":
            w2, shown, _ = await _wait_for_ui(client, app_id, 15)  # don't press OK into a loading screen
            waited, app, state, ids = await _wait_for_playback(client, app_id, 1)
            if state != "playing" and shown:
                report["screen_before_ok"] = str(CAPTURE_DIR / "probe.jpg")
                await _press_ok(client, report, "nothing playing and a screen is up (profile picker?)")
                waited, app, state, ids = await _wait_for_playback(client, app_id, 15)
        confirmed = state == "playing"
    else:  # "details"
        # The first second or two still shows the webOS launcher animating, which looks like UI.
        await asyncio.sleep(5)
        waited, shown, ids = await _wait_for_ui(client, app_id, max(delay, 40))
        report.update(waited_s=waited + 5, ui_shown=shown)
        if not shown:
            report["result"] = "app still on a blank/loading screen; nothing pressed"
            report["screen_after"] = str(CAPTURE_DIR / "probe.jpg")
            return report
        if not ids:  # UI up and no trailer playing: the profile picker
            (CAPTURE_DIR / "probe.jpg").replace(CAPTURE_DIR / "profile.jpg")
            report["screen_before_ok"] = str(CAPTURE_DIR / "profile.jpg")
            await _press_ok(client, report, "profile screen")
            # The title page starts its trailer within ~10 s; that media id marks "on the title page".
            _, _, _, ids = await _wait_for_playback(client, app_id, 20)
            if not ids:
                await _wait_for_ui(client, app_id, 10)
        report["trailer_seen"] = bool(ids)
        await _press_ok(client, report, "Play on the title page")
        # The film plays under a media id other than the trailer's. Without a trailer to compare
        # against, also require the UI to be gone (full-screen video captures as black).
        confirmed = False
        loop = asyncio.get_running_loop()
        end = loop.time() + 20
        app, state = None, None
        while loop.time() < end:
            await asyncio.sleep(2)
            app, media = await _foreground(client)
            new = _media_ids(media, app_id) - ids
            state = "playing" if new else _playing(media, app_id)
            if new and (ids or not (await _ui_shown(client, "probe"))[0]):
                confirmed = True
                break

    report["foreground_app_after"] = app
    report["play_state_after"] = state
    if confirmed:
        report["result"] = "playing"
    else:
        report["result"] = "not confirmed playing"
        report["screen_after"] = await capture_screen(client, "after")
        report["hint"] = ("Look at screen_after (Read the image): a profile picker, a title page for something "
                          "not streaming in this region, or still loading. Use press_button to finish.")
    return report


CAPTURE_DIR = titles.CONFIG_DIR / "captures"


async def capture_screen(client, name: str = "screen") -> str | None:
    """Save a JPEG of what the TV shows (UI layer only: video frames come out black). Returns the path."""
    import ssl

    import aiohttp

    try:
        r = await client.request("tv/executeOneShot", {"path": "/tmp/capture.jpg", "method": "DISPLAY",
                                                      "format": "JPG"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the TV's self-signed cert, on the LAN
        async with aiohttp.ClientSession() as s, s.get(r["imageUri"], ssl=ctx,
                                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.read()
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        path = CAPTURE_DIR / f"{name}.jpg"
        path.write_bytes(data)
        return str(path)
    except Exception as e:  # noqa: BLE001 - a capture is a nice-to-have
        log.warning("screen capture failed: %s", e)
        return None


async def play_title(client, *, title: str | None, tmdb_id: int | None, media_type: str,
                     app: str | None, season: int | None, episode: int | None,
                     app_content_id: str | None, open_without_id: bool,
                     profile_ok: bool, profile_delay: float) -> dict[str, Any]:
    installed = installed_services(client)
    want = service_key(app) if app else None
    if app and not want:
        return {"status": "unknown_app", "message": f"{app!r} isn't a streaming app play_title knows. "
                f"Known: {', '.join(s.name for s in APPS.values())}."}
    if want and want not in installed:
        return {"status": "not_installed", "app": APPS[want].name,
                "installed_streaming_apps": [APPS[k].name for k in installed],
                "message": f"{APPS[want].name} isn't installed on the TV."}

    episode_mode = media_type == "tv" and season is not None and episode is not None
    info: dict[str, Any] = {}
    if app_content_id:
        if not want:
            return {"status": "error", "message": "app_content_id needs app as well."}
        key = titles.cache_key(media_type, tmdb_id, title, season, episode)
        titles.remember(key, want, app_content_id, label=title)
        info = {"key": key, "label": title, "ids": {want: app_content_id}, "sources": {want: "manual"}}
    elif tmdb_id or title:
        try:
            info = await titles.resolve(title=title, tmdb_id=tmdb_id, media_type=media_type,
                                        season=season, episode=episode)
        except Exception as e:  # noqa: BLE001 - Wikidata down shouldn't stop us opening the app
            log.warning("wikidata lookup failed: %s", e)
            info = {"ids": {}, "sources": {}, "lookup_error": str(e)}
    else:
        return {"status": "error", "message": "Give a title and/or tmdb_id."}

    name = info.get("label") or title or f"TMDB {tmdb_id}"
    if episode_mode:
        name = f"{name} S{season:02d}E{episode:02d}"
        if info.get("episode_label"):
            name += f" – {info['episode_label']}"
    ids: dict[str, str] = dict(info.get("ids", {}))
    series_ids: dict[str, str] = dict(info.get("series_ids", {}))

    # Pick the app: the requested one, else the first installed app we have an id for, else ask.
    if want:
        svc = want
    else:
        svc = next((k for k in APPS if k in installed and k in ids), None)
        if svc is None:
            return {"status": "needs_lookup", "title": name, "wikidata": info.get("wikidata"),
                    "tmdb_id": info.get("tmdb_id", tmdb_id), "found_ids": ids, "series_ids": series_ids,
                    "installed_streaming_apps": [APPS[k].name for k in installed],
                    "message": "No content id for an installed app. Find where it streams (e.g. Screenward "
                               "get_watch_providers for IN), then call again with app=..., and app_content_id "
                               "from a web search, or open_without_id=true to just open the app.",
                    "id_hints": {APPS[k].name: APPS[k].id_hint for k in installed}}
    spec = APPS[svc]
    app_id = installed[svc]

    cid = ids.get(svc)
    used_series_id = False
    if cid is None and svc == "youtube":
        cid = "search:" + (f"{name} full movie" if media_type == "movie" else name)
    if cid is None and episode_mode and series_ids.get(svc) and spec.deep_link:
        cid, used_series_id = series_ids[svc], True

    if spec.deep_link and cid is None and not open_without_id:
        return {"status": "needs_lookup", "app": spec.name, "title": name, "wikidata": info.get("wikidata"),
                "tmdb_id": info.get("tmdb_id", tmdb_id), "found_ids": ids, "series_ids": series_ids,
                "message": f"No {spec.name} id on Wikidata for {name}. Web-search it ({spec.id_hint}), "
                           f"then call play_title again with app={svc!r} and app_content_id=<id>; it is cached "
                           "after that. Or pass open_without_id=true to open the app with a toast.",
                "id_hint": spec.id_hint}

    payload: dict[str, Any] = {"id": app_id}
    deep = bool(spec.deep_link and cid)
    if deep:
        payload.update(spec.deep_link(cid))
    await client.request(ep.LAUNCH, payload)

    result: dict[str, Any] = {"app": spec.name, "app_id": app_id, "title": name,
                              "content_id": cid, "id_source": info.get("sources", {}).get(svc)}
    if deep:
        result["status"] = "search_opened" if cid.startswith("search:") else "deep_linked"
        result["launch"] = payload
        if used_series_id:
            result["note"] = (f"No {spec.name} id for that episode, so the show was opened (it usually resumes "
                              "or starts at S1E1). For the exact episode, web-search the episode id "
                              f"({spec.id_hint}) and pass app_content_id.")
        if profile_ok and svc != "youtube":
            try:
                result["playback"] = await _start_playback(client, app_id, spec.lands_on, profile_delay)
            except WebOsTvCommandError as e:
                result["playback"] = {"error": str(e)}
    else:
        result["status"] = "opened_app"
        await asyncio.sleep(3)
        msg = f"Find: {name}" if not spec.deep_link else f"Search for: {name}"
        try:
            await client.send_message(msg[:200])
            result["toast"] = msg
        except WebOsTvCommandError:
            pass
        result["note"] = (f"{spec.name} has no working deep link on this TV, so it was opened with a toast; "
                          "use the app's search to start it." if not spec.deep_link else
                          f"Opened {spec.name} without a content id.")
    _log_play(info.get("tmdb_id") or tmdb_id, media_type, name, spec.name, season, episode, result["status"])
    return result


HISTORY_FILE = titles.CONFIG_DIR / "watch_history.json"


def _log_play(tmdb_id, media_type, name, app_name, season, episode, status) -> None:
    """Append to ~/.config/lgtv/watch_history.json (used by /movienight to skip recent picks)."""
    try:
        try:
            hist = json.loads(HISTORY_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            hist = []
        hist.append({"at": datetime.now().isoformat(timespec="seconds"), "tmdb_id": tmdb_id,
                     "media_type": media_type, "title": name, "app": app_name,
                     "season": season, "episode": episode, "status": status})
        titles.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(hist[-500:], indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("could not write watch history: %s", e)
