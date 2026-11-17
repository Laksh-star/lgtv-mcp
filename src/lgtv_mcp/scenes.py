"""Scene capture: freeze what's on the TV and keep a scene card (for recreating scenes later).

What a TV screenshot holds (tested 17 Nov 2026, see README "Scene capture"):
- Netflix / Prime Video: the picture is black (protected video), but subtitle lines and the paused
  player bar (title, position / duration) are captured.
- YouTube and HDMI inputs (set-top box): the full picture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime
from typing import Any

from aiowebostv.exceptions import WebOsTvCommandError

from . import play
from .tv import CONFIG_DIR

log = logging.getLogger("lgtv_mcp.scenes")

SCENES_DIR = CONFIG_DIR / "scenes"
SCENES_FILE = SCENES_DIR / "scenes.json"

PROTECTED = {"netflix", "amazon", "hotstar", "com.zee5.app"}  # picture comes out black
LIVE_PREFIXES = ("com.webos.app.hdmi", "com.webos.app.livetv")  # can't be paused from here


def _load() -> list[dict[str, Any]]:
    try:
        return json.loads(SCENES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save(cards: list[dict[str, Any]]) -> None:
    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SCENES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cards, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(SCENES_FILE)


def _now_playing(app_id: str, app_title: str) -> dict[str, Any]:
    """What play_title last started in this app (title, TMDB id), from watch_history.json."""
    try:
        hist = json.loads(play.HISTORY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    for h in reversed(hist[-50:]):
        if h.get("app_id") == app_id or h.get("app") == app_title:
            return {k: h.get(k) for k in ("title", "tmdb_id", "media_type", "season", "episode")}
    return {}


async def _set_paused(client, app_id: str) -> str | None:
    """Pause through the media API and confirm it. Returns the final play state."""
    state = None
    for _ in range(2):
        await client.pause()
        for _ in range(6):
            await asyncio.sleep(0.5)
            _, media = await play._foreground(client)
            state = play._playing(media, app_id)
            if state == "paused":
                return state
    return state


async def capture_scene(client, note: str | None, keep_paused: bool) -> tuple[dict[str, Any], dict[str, str]]:
    app_id, media = await play._foreground(client)
    app_id = app_id or client.tv_state.current_app_id or "unknown"
    titles_by_id = {i: (a.get("title") or i) for i, a in client.tv_state.apps.items()}
    app_title = titles_by_id.get(app_id, app_id)
    live = app_id.startswith(LIVE_PREFIXES)
    state_before = play._playing(media, app_id)

    # Two captures: the moment itself (clean frame, or the subtitle line in streaming apps), then
    # paused (the player bar adds title and timecode but covers part of the picture).
    moment = await play.capture_screen(client, "scene_moment")
    paused_here = False
    path = None
    if not live and state_before == "playing":
        state = await _set_paused(client, app_id)
        paused_here = state == "paused"
        await asyncio.sleep(1.0)  # let the player bar (title, timecode) draw
        path = await play.capture_screen(client, "scene_paused")
    if paused_here and not keep_paused:
        try:
            await client.play()
        except WebOsTvCommandError as e:
            log.warning("resume failed: %s", e)

    at = datetime.now()
    scene_id = at.strftime("%Y%m%d-%H%M%S")
    images: dict[str, str] = {}
    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    for kind, src in (("moment", moment), ("paused", path)):
        if src:
            images[kind] = str(SCENES_DIR / f"{scene_id}-{kind}.jpg")
            shutil.copyfile(src, images[kind])

    card: dict[str, Any] = {
        "id": scene_id,
        "at": at.isoformat(timespec="seconds"),
        "app": app_title,
        "app_id": app_id,
        **_now_playing(app_id, app_title),
        "picture": "black (protected video): text only" if app_id in PROTECTED else "full frame",
        "play_state_before": state_before,
        "paused_for_capture": paused_here,
        "resumed": paused_here and not keep_paused,
        "note": note,
        "timecode": None,
        "dialogue": None,
        "description": None,
        "images": images,
    }
    if live:
        card["picture"] = "full frame (live input, not paused)"
    cards = _load()
    cards.append(card)
    _save(cards)
    return card, images


def annotate_scene(scene_id: str, **fields: Any) -> dict[str, Any]:
    cards = _load()
    for card in cards:
        if card["id"] == scene_id:
            card.update({k: v for k, v in fields.items() if v is not None})
            _save(cards)
            return card
    raise KeyError(scene_id)


def list_scenes(title: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    cards = _load()
    if title:
        t = title.lower()
        cards = [c for c in cards if t in (c.get("title") or "").lower() or t in (c.get("note") or "").lower()]
    return cards[-limit:][::-1]
