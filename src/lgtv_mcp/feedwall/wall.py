"""Refresh a profile's sources and assemble what the wall shows.

The wall is always rebuilt from each source's latest fetch, so a post that was deleted drops off
(and an edited one updates) at the next refresh. A source that fails keeps its last good posts,
marked with when they were last updated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import MIN_REFRESH_MINUTES, Config, Profile, Source
from .moderation import Verdict, moderate
from .sources import FETCHERS, Post, reason, session
from .store import DECISIONS_FILE, load_source, locked_json, read_decisions, register_images, save_source

log = logging.getLogger("feedwall")
_locks: dict[str, asyncio.Lock] = {}


def short_id(post_id: str) -> str:
    return hashlib.sha1(post_id.encode()).hexdigest()[:6]


def _interval(cfg: Config, src: Source) -> float:
    return max(cfg.refresh_minutes, MIN_REFRESH_MINUTES.get(src.kind, 0)) * 60


async def _refresh_one(s, cfg: Config, src: Source, force: bool) -> None:
    prev = load_source(src.id)
    if not force and time.time() - prev.get("tried_at", 0) < _interval(cfg, src):
        return
    entry = {**prev, "tried_at": time.time(), "kind": src.kind}
    try:
        posts, meta = await FETCHERS[src.kind](s, cfg, src, prev)
    except Exception as e:  # noqa: BLE001 - one source failing must not stop the others
        entry["error"] = reason(e)
        log.error("source %s failed: %s%s", src.id, entry["error"],
                  " (showing its last good posts)" if prev.get("posts") else "")
    else:
        if posts is not None:  # None = not modified since the last fetch
            entry["posts"] = [p.to_dict() for p in posts]
        entry.update(ok_at=time.time(), error="", meta=meta)
        log.info("source %s: %d posts", src.id, len(entry.get("posts") or []))
    save_source(src.id, entry)


async def refresh(cfg: Config, profile: Profile, force: bool = False) -> None:
    lock = _locks.setdefault(profile.name, asyncio.Lock())
    async with lock:
        async with session() as s:
            await asyncio.gather(*(_refresh_one(s, cfg, src, force) for src in cfg.sources_for(profile)))


@dataclass
class Wall:
    profile: Profile
    shown: list[Post] = field(default_factory=list)
    pending: list[Post] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)   # reason -> count
    sources: list[dict[str, Any]] = field(default_factory=list)


def build(cfg: Config, profile: Profile, decisions: dict[str, Any] | None = None) -> Wall:
    decisions = read_decisions() if decisions is None else decisions
    wall = Wall(profile)
    seen: set[str] = set()
    for src in cfg.sources_for(profile):
        entry = load_source(src.id)
        posts = [Post.from_dict(p) for p in entry.get("posts") or []]
        wall.sources.append({
            "id": src.id, "name": src.label, "kind": src.kind, "posts": len(posts),
            "ok": not entry.get("error") and bool(entry.get("ok_at")),
            "error": entry.get("error") or ("" if entry.get("ok_at") else "not fetched yet"),
            "last_updated": entry.get("ok_at"),
        })
        for p in posts:
            if p.id in seen:
                continue
            seen.add(p.id)
            v: Verdict = moderate(p, profile, src, decisions)
            if v.action == "show":
                wall.shown.append(p)
            elif v.action == "pending":
                wall.pending.append(p)
            else:
                wall.dropped[v.reason] = wall.dropped.get(v.reason, 0) + 1
    newest = lambda p: p.created or ""  # noqa: E731
    wall.pending.sort(key=newest, reverse=True)
    wall.shown = interleave(wall.shown, cfg.max_posts)
    return wall


def interleave(posts: list[Post], limit: int) -> list[Post]:
    """Newest first within each source, taking one from each source in turn, so a busy hashtag
    doesn't crowd out a channel that posts once a day."""
    by_src: dict[str, list[Post]] = {}
    for p in posts:
        by_src.setdefault(p.source, []).append(p)
    queues = [sorted(v, key=lambda p: p.created or "", reverse=True) for v in by_src.values()]
    queues.sort(key=lambda q: q[0].created or "", reverse=True)
    out: list[Post] = []
    while len(out) < limit and any(queues):
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


def _img(url: str) -> str:
    from .store import image_key
    return f"/wall/img/{image_key(url)}" if url.startswith("https://") else ""


def _tz_name() -> str:
    """The briefing's time zone, so the wall's clock doesn't depend on the TV's own clock settings."""
    try:
        from ..briefing.config import load_config as load_briefing_config
        return load_briefing_config().tz.key
    except Exception:  # noqa: BLE001 - fall back to the TV's local time
        return ""


def page_data(cfg: Config, wall: Wall, session_state: dict[str, Any] | None) -> dict[str, Any]:
    """What /wall/data sends to the TV. Images go through the Mac (/wall/img/…), so the TV only talks to it."""
    from .page import qr_svg

    register_images([u for p in wall.shown for u in (p.image, p.avatar)])
    posts = []
    for p in wall.shown:
        posts.append({
            "id": short_id(p.id), "platform": p.platform, "source": p.source,
            "name": p.author_name, "handle": p.author_handle, "avatar": _img(p.avatar),
            "text": p.text, "created": p.created, "day_only": p.day_only,
            "image": _img(p.image), "alt": p.image_alt, "video": p.video, "live": p.live,
            "hidden_media": p.has_hidden_media, "reposted_by": p.reposted_by,
            "qr": qr_svg(p.url) if p.url else "",
        })
    return {
        "title": wall.profile.title, "profile": wall.profile.name, "rotate_seconds": cfg.rotate_seconds,
        "server_ms": int(time.time() * 1000), "tz": _tz_name(), "ends_at": (session_state or {}).get("ends_at"),
        "approval": wall.profile.approval, "pending": len(wall.pending),
        "sources": [{k: s[k] for k in ("name", "ok", "error", "last_updated", "posts")} for s in wall.sources],
        "posts": posts,
    }


# ---------------------------------------------------------------- approval queue


def pending_list(wall: Wall, limit: int = 20) -> list[dict[str, Any]]:
    return [{
        "id": short_id(p.id), "source": p.source, "platform": p.platform,
        "author": f"{p.author_name} ({p.author_handle})" if p.author_handle else p.author_name,
        "posted": p.created, "text": p.text[:400] + ("…" if len(p.text) > 400 else ""),
        "image": ("video" if p.video else "image") + (f" — alt: {p.image_alt[:120]}" if p.image_alt else " (no alt text)") if p.image else "",
        "link": p.url,
    } for p in wall.pending[:limit]]


def decide(wall: Wall, status: str, ids: list[str] | None, newest: int | None) -> tuple[list[str], list[str]]:
    """Approve or reject pending posts by short id, or the `newest` N. Returns (done ids, unknown ids)."""
    by_short = {short_id(p.id): p for p in wall.pending}
    if status == "rejected":  # rejecting something already on the wall is allowed too
        by_short.update({short_id(p.id): p for p in wall.shown})
    chosen: list[Post] = []
    unknown: list[str] = []
    for i in ids or []:
        p = by_short.get(i.strip().lower())
        (chosen.append(p) if p else unknown.append(i))
    if newest:
        chosen += [p for p in wall.pending[:newest] if p not in chosen]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with locked_json(DECISIONS_FILE) as d:
        posts = d.setdefault("posts", {})
        for p in chosen:
            posts[p.id] = {"status": status, "rev": p.rev, "at": now, "source": p.source}
    return [short_id(p.id) for p in chosen], unknown
