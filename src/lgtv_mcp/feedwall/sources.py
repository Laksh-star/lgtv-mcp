"""Fetch and normalise posts from each kind of source.

Every source turns into a list of Post. The parse_* functions are pure (tested against recorded
fixtures); the fetch_* functions do the network calls. Each source fails on its own, like the
briefing's: wall.refresh keeps a failed source's last good posts and records why it failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
import feedparser

from .config import KC_BLUESKY, KC_BLUESKY_SESSION, KC_YOUTUBE, Config, Source, keychain_get, keychain_set
from .store import CACHE_DIR

log = logging.getLogger("feedwall")
UA = "lgtv-feedwall/0.1 (+home use)"
TIMEOUT = aiohttp.ClientTimeout(total=15)
BSKY_PUBLIC = "https://public.api.bsky.app/xrpc"
X_OEMBED = "https://publish.x.com/oembed"
YT_API = "https://www.googleapis.com/youtube/v3"


class SourceError(Exception):
    """A failure whose message is fit to show in status and on the wall."""


@dataclass
class Post:
    id: str                    # stable across fetches, e.g. "bsky:at://…", "masto:https://…", "yt:<video>"
    platform: str              # bluesky | mastodon | rss | youtube | x
    source: str = ""           # source id
    url: str = ""              # link for the QR code
    author_name: str = ""
    author_handle: str = ""    # shown as written (e.g. @nasa.gov, @user@host)
    author_keys: list[str] = field(default_factory=list)  # lower-case ids the allow/blocklists match against
    avatar: str = ""
    text: str = ""
    created: str = ""          # ISO 8601, UTC
    day_only: bool = False     # only the date is known (X embeds)
    image: str = ""
    image_alt: str = ""
    video: bool = False        # the media is a video: show its still, never play it
    live: bool = False
    has_hidden_media: bool = False  # media we can't show (X photos): the card says "scan to see"
    card_title: str = ""       # link preview title (checked by the blocklist)
    labels: list[str] = field(default_factory=list)
    visibility: str = "public"
    sensitive: bool = False
    content_warning: str = ""
    bot: bool = False
    discoverable: bool = True  # False when the account opted out of discovery/search
    is_reply: bool = False
    reposted_by: str = ""      # handle of the account that reposted it, if it's a repost
    reposted_by_keys: list[str] = field(default_factory=list)
    quoted_keys: list[str] = field(default_factory=list)  # authors of an embedded/quoted post
    rev: str = ""              # changes when the post is edited

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Post:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


def _iso(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rev(*parts: Any) -> str:
    import hashlib
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = re.sub(r"(?i)</p>\s*<p[^>]*>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ---------------------------------------------------------------- Bluesky


def _bsky_keys(author: dict[str, Any]) -> list[str]:
    return [k.lower() for k in (author.get("handle", ""), author.get("did", "")) if k]


def _bsky_media(embed: dict[str, Any] | None) -> tuple[str, str, bool, str]:
    """(image url, alt, is_video, link card title) from a post's embed view."""
    if not embed:
        return "", "", False, ""
    t = embed.get("$type", "")
    if t.startswith("app.bsky.embed.recordWithMedia"):
        return _bsky_media(embed.get("media"))
    if t.startswith("app.bsky.embed.images"):
        imgs = embed.get("images") or []
        return (imgs[0].get("thumb") or imgs[0].get("fullsize", ""), imgs[0].get("alt", ""), False, "") if imgs else ("", "", False, "")
    if t.startswith("app.bsky.embed.gallery"):
        items = embed.get("items") or []
        return (items[0].get("thumbnail") or items[0].get("fullsize", ""), items[0].get("alt", ""), False, "") if items else ("", "", False, "")
    if t.startswith("app.bsky.embed.video"):
        return embed.get("thumbnail", ""), embed.get("alt", ""), True, ""
    if t.startswith("app.bsky.embed.external"):
        ext = embed.get("external") or {}
        return ext.get("thumb", ""), "", False, ext.get("title", "")
    return "", "", False, ""


def _bsky_quoted(embed: dict[str, Any] | None) -> list[str]:
    if not embed:
        return []
    t = embed.get("$type", "")
    rec = embed.get("record") if t.startswith("app.bsky.embed.record#") else (
        (embed.get("record") or {}).get("record") if t.startswith("app.bsky.embed.recordWithMedia") else None)
    return _bsky_keys(rec.get("author") or {}) if isinstance(rec, dict) else []


def parse_bsky_post(post: dict[str, Any], reason: dict[str, Any] | None = None, source: str = "") -> Post:
    author, record = post.get("author") or {}, post.get("record") or {}
    labels = [lab.get("val", "") for lab in (post.get("labels") or []) + (author.get("labels") or [])]
    image, alt, video, card = _bsky_media(post.get("embed"))
    rkey = post["uri"].rsplit("/", 1)[-1]
    handle = author.get("handle", "")
    repost_by = (reason or {}).get("by") if (reason or {}).get("$type", "").endswith("reasonRepost") else None
    text = record.get("text", "")
    return Post(
        id=f"bsky:{post['uri']}", platform="bluesky", source=source,
        url=f"https://bsky.app/profile/{handle if handle and handle != 'handle.invalid' else author.get('did', '')}/post/{rkey}",
        author_name=author.get("displayName") or handle, author_handle=f"@{handle}", author_keys=_bsky_keys(author),
        avatar=author.get("avatar", ""), text=text, created=_iso(record.get("createdAt") or post.get("indexedAt")),
        image=image, image_alt=alt, video=video, card_title=card, labels=[v for v in labels if v],
        bot="bot" in labels, is_reply=bool(record.get("reply")),
        reposted_by=f"@{repost_by['handle']}" if repost_by else "",
        reposted_by_keys=_bsky_keys(repost_by) if repost_by else [],
        quoted_keys=_bsky_quoted(post.get("embed")), rev=_rev(post.get("cid"), text, alt),
    )


def parse_bsky_feed(data: dict[str, Any], source: str = "") -> list[Post]:
    """getAuthorFeed / getFeed / getTimeline ({"feed": [{post, reason}]}) or searchPosts ({"posts": [...]})."""
    if "feed" in data:
        return [parse_bsky_post(it["post"], it.get("reason"), source) for it in data["feed"] if it.get("post")]
    return [parse_bsky_post(p, None, source) for p in data.get("posts", [])]


async def _bsky_public(s: aiohttp.ClientSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
    async with s.get(f"{BSKY_PUBLIC}/{method}", params=params) as r:
        if r.status == 403:
            raise SourceError(f"Bluesky refused {method} without a login (HTTP 403)")
        if r.status == 400:
            raise SourceError(f"Bluesky: {(await r.json(content_type=None)).get('message', 'bad request')}")
        r.raise_for_status()
        return await r.json()


@contextlib.contextmanager
def _login_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_DIR / ".bluesky-login.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


class BlueskyAuth:
    """A Bluesky login made with an app password. The session is saved in the Keychain and refreshed,
    because createSession is limited (we saw 10 per day) and must not run on every refresh."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _load(self) -> dict[str, Any] | None:
        item = keychain_get(KC_BLUESKY_SESSION)
        try:
            return json.loads(item[1]) if item else None
        except ValueError:
            return None

    def _save(self, sess: dict[str, Any]) -> None:
        keep = {k: sess[k] for k in ("accessJwt", "refreshJwt", "did", "handle") if k in sess}
        pds = next((svc.get("serviceEndpoint") for svc in (sess.get("didDoc") or {}).get("service", [])
                    if svc.get("id") == "#atproto_pds"), None)
        keep["pds"] = (pds or sess.get("pds") or self.cfg.bluesky_service).rstrip("/")
        keychain_set(KC_BLUESKY_SESSION, keep.get("handle") or "session", json.dumps(keep, separators=(",", ":")))
        self._sess = keep

    async def _create(self, s: aiohttp.ClientSession) -> dict[str, Any]:
        cred = await asyncio.to_thread(keychain_get, KC_BLUESKY)
        if not cred or not cred[0]:
            raise SourceError(f"no Bluesky app password in the Keychain (service '{KC_BLUESKY}', account = your handle) — see README")
        async with s.post(f"{self.cfg.bluesky_service}/xrpc/com.atproto.server.createSession",
                          json={"identifier": cred[0], "password": cred[1]}) as r:
            body = await r.json(content_type=None)
            if r.status == 429:
                raise SourceError("Bluesky login rate limit reached (about 10 logins a day); try again tomorrow")
            if r.status != 200:
                raise SourceError(f"Bluesky login failed: {body.get('message', r.status)}")
        log.info("bluesky: logged in as %s", body.get("handle"))
        await asyncio.to_thread(self._save, body)
        return self._sess

    async def _refresh(self, s: aiohttp.ClientSession, sess: dict[str, Any]) -> dict[str, Any] | None:
        async with s.post(f"{sess['pds']}/xrpc/com.atproto.server.refreshSession",
                          headers={"Authorization": f"Bearer {sess['refreshJwt']}"}) as r:
            if r.status != 200:
                return None
            body = await r.json()
        body.setdefault("pds", sess["pds"])
        await asyncio.to_thread(self._save, body)
        return self._sess

    async def get(self, s: aiohttp.ClientSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
        sess = await asyncio.to_thread(self._load)
        for attempt in range(3):
            if sess is None:
                with _login_lock():
                    sess = await asyncio.to_thread(self._load) or await self._create(s)
            async with s.get(f"{sess['pds']}/xrpc/{method}", params=params,
                             headers={"Authorization": f"Bearer {sess['accessJwt']}"}) as r:
                if r.status == 200:
                    return await r.json()
                body = await r.json(content_type=None) if r.content_type.endswith("json") else {}
                if r.status in (400, 401) and body.get("error") in ("ExpiredToken", "InvalidToken") and attempt < 2:
                    with _login_lock():
                        sess = await self._refresh(s, sess)  # None -> log in again on the next pass
                    continue
                raise SourceError(f"Bluesky {method}: {body.get('message') or f'HTTP {r.status}'}")
        raise SourceError("Bluesky login kept expiring")


async def _resolve_feed_uri(s: aiohttp.ClientSession, feed: str) -> str:
    if feed.startswith("at://"):
        return feed
    m = re.match(r"https://bsky\.app/profile/([^/]+)/feed/([^/?#]+)", feed)
    if not m:
        raise SourceError("feed should be an at:// address or a bsky.app/profile/…/feed/… link")
    actor = m.group(1)
    if not actor.startswith("did:"):
        actor = (await _bsky_public(s, "com.atproto.identity.resolveHandle", {"handle": actor}))["did"]
    return f"at://{actor}/app.bsky.feed.generator/{m.group(2)}"


async def fetch_bluesky(s: aiohttp.ClientSession, cfg: Config, src: Source, prev: dict[str, Any]) -> tuple[list[Post], dict]:
    n = min(max(src.count, 1), 100)
    p = src.params
    if src.kind == "bluesky_author":
        data = await _bsky_public(s, "app.bsky.feed.getAuthorFeed",
                                  {"actor": p["actor"].lstrip("@"), "limit": n, "filter": "posts_no_replies"})
    elif src.kind == "bluesky_feed":
        meta = prev.get("meta") or {}
        uri = meta.get("feed_uri") if meta.get("feed") == p["feed"] else None
        uri = uri or await _resolve_feed_uri(s, p["feed"])
        data = await _bsky_public(s, "app.bsky.feed.getFeed", {"feed": uri, "limit": n})
        return parse_bsky_feed(data, src.id), {"feed": p["feed"], "feed_uri": uri}
    elif src.kind == "bluesky_search":
        # public.api.bsky.app answers searchPosts with 403, so search goes through your login.
        data = await BlueskyAuth(cfg).get(s, "app.bsky.feed.searchPosts", {"q": p["query"], "sort": "latest", "limit": n})
    else:  # bluesky_timeline
        data = await BlueskyAuth(cfg).get(s, "app.bsky.feed.getTimeline", {"limit": n})
    return parse_bsky_feed(data, src.id), {}


# ---------------------------------------------------------------- Mastodon


def _masto_acct(acct: str, instance: str) -> str:
    return acct if "@" in acct else f"{acct}@{instance}"


def parse_mastodon_status(st: dict[str, Any], instance: str, source: str = "") -> Post:
    outer = st
    st = st.get("reblog") or st
    acc = st.get("account") or {}
    acct = _masto_acct(acc.get("acct", ""), instance)
    media = next((m for m in st.get("media_attachments") or [] if m.get("type") in ("image", "gifv", "video")), None)
    image = (media.get("url") if media and media.get("type") == "image" else media.get("preview_url")) if media else ""
    card = st.get("card") or {}
    if not image and card.get("image"):
        image = card["image"]
    quote = st.get("quote") or {}
    quoted = (quote.get("quoted_status") or {}).get("account") if isinstance(quote, dict) else None
    text = html_to_text(st.get("content", ""))
    by = outer.get("account") if outer.get("reblog") else None
    return Post(
        id=f"masto:{st.get('uri') or st.get('url')}", platform="mastodon", source=source, url=st.get("url") or st.get("uri", ""),
        author_name=acc.get("display_name") or acc.get("username", ""), author_handle=f"@{acct}",
        author_keys=[acct.lower()], avatar=acc.get("avatar_static") or acc.get("avatar", ""), text=text,
        created=_iso(st.get("created_at")), image=image or "",
        image_alt=(media.get("description") or "") if media else "", video=bool(media and media.get("type") != "image"),
        card_title=card.get("title", ""),
        # Either the reblog or the original being non-public keeps it off the wall.
        visibility=st.get("visibility", "public") if outer.get("visibility", "public") == "public" else outer["visibility"],
        sensitive=bool(st.get("sensitive")), content_warning=st.get("spoiler_text", ""), bot=bool(acc.get("bot")),
        discoverable=not acc.get("noindex") and acc.get("discoverable") is not False,
        is_reply=bool(st.get("in_reply_to_id")),
        reposted_by=f"@{_masto_acct(by.get('acct', ''), instance)}" if by else "",
        reposted_by_keys=[_masto_acct(by.get("acct", ""), instance).lower()] if by else [],
        quoted_keys=[_masto_acct(quoted.get("acct", ""), instance).lower()] if quoted else [],
        rev=_rev(st.get("edited_at"), text, st.get("spoiler_text"), [m.get("description") for m in st.get("media_attachments") or []]),
    )


def parse_mastodon(statuses: list[dict[str, Any]], instance: str, source: str = "") -> list[Post]:
    return [parse_mastodon_status(st, instance, source) for st in statuses]


async def _masto_get(s: aiohttp.ClientSession, instance: str, path: str, params: dict[str, Any] | None = None) -> Any:
    async with s.get(f"https://{instance}{path}", params=params) as r:
        if r.status in (401, 403, 422):
            msg = (await r.json(content_type=None) or {}).get("error", "") if r.content_type.endswith("json") else ""
            raise SourceError(f"{instance} needs a login for this ({msg or f'HTTP {r.status}'})")
        if r.status == 404:
            raise SourceError(f"{instance}: not found")
        if r.status == 429:
            raise SourceError(f"{instance} rate limit reached (300 requests per 5 minutes)")
        r.raise_for_status()
        return await r.json()


async def fetch_mastodon(s: aiohttp.ClientSession, cfg: Config, src: Source, prev: dict[str, Any]) -> tuple[list[Post], dict]:
    inst = src.params["instance"].removeprefix("https://").strip("/")
    n = min(max(src.count, 1), 40)
    if src.kind == "mastodon_tag":
        tag = src.params["tag"].lstrip("#")
        data = await _masto_get(s, inst, f"/api/v1/timelines/tag/{quote(tag)}", {"limit": n})
        return parse_mastodon(data, inst, src.id), {}
    acct = src.params["account"].lstrip("@")
    meta = prev.get("meta") or {}
    acc_id = meta.get("account_id") if meta.get("account") == acct else None
    if not acc_id:
        acc_id = (await _masto_get(s, inst, "/api/v1/accounts/lookup", {"acct": acct}))["id"]
    data = await _masto_get(s, inst, f"/api/v1/accounts/{acc_id}/statuses", {"limit": n, "exclude_replies": "true"})
    return parse_mastodon(data, inst, src.id), {"account": acct, "account_id": acc_id}


# ---------------------------------------------------------------- RSS / Atom and YouTube


def _entry_image(e: Any) -> str:
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key) or []:
            if m.get("url") and (key == "media_thumbnail" or str(m.get("medium", "image")) == "image"):
                return m["url"]
    for link in e.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href", "")
    return ""


def _entry_time(e: Any) -> str:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat(timespec="seconds") if t else ""


def parse_rss(body: bytes, source: Source) -> list[Post]:
    d = feedparser.parse(body)
    if d.bozo and not d.entries:
        raise SourceError(f"couldn't read the feed ({d.bozo_exception})")
    feed_title = html_to_text(d.feed.get("title", "")) or source.label
    host = urlparse(d.feed.get("link") or source.params.get("url", "")).netloc
    icon = d.feed.get("image", {}).get("href", "") if isinstance(d.feed.get("image"), dict) else ""
    out = []
    for e in d.entries[: source.count]:
        title = html_to_text(e.get("title", ""))
        summary = html_to_text(e.get("summary", ""))
        text = title or summary
        if title and summary and summary != title and not summary.startswith(title):
            text = f"{title}\n\n{summary}"
        link = e.get("link", "")
        author = html_to_text(e.get("author", ""))
        out.append(Post(
            id=f"rss:{e.get('id') or link or title}", platform="rss", source=source.id, url=link,
            author_name=feed_title, author_handle=author or host, author_keys=[host.lower()] if host else [],
            avatar=icon, text=text[:1000], created=_entry_time(e), image=_entry_image(e),
            rev=_rev(title, summary),
        ))
    return out


def parse_youtube_rss(body: bytes, source: Source) -> list[Post]:
    d = feedparser.parse(body)
    if d.bozo and not d.entries:
        raise SourceError(f"couldn't read the YouTube feed ({d.bozo_exception})")
    out = []
    for e in d.entries[: source.count]:
        vid = e.get("yt_videoid", "")
        chan = e.get("yt_channelid", "")
        if chan and not chan.startswith("UC"):
            chan = "UC" + chan
        title = html_to_text(e.get("title", ""))
        out.append(Post(
            id=f"yt:{vid}", platform="youtube", source=source.id, url=e.get("link") or f"https://www.youtube.com/watch?v={vid}",
            author_name=e.get("author", "") or html_to_text(d.feed.get("title", "")), author_handle="YouTube",
            author_keys=[f"youtube:{chan}".lower(), chan.lower()] if chan else [], text=title,
            created=_entry_time(e), image=_entry_image(e), image_alt=f"Video thumbnail: {title}", video=True,
            rev=_rev(title),
        ))
    return out


def parse_youtube_search(data: dict[str, Any], source: Source, avatars: dict[str, str] | None = None) -> list[Post]:
    avatars = avatars or {}
    out = []
    for it in data.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        chan = sn.get("channelId", "")
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        title = html.unescape(sn.get("title", ""))
        out.append(Post(
            id=f"yt:{vid}", platform="youtube", source=source.id, url=f"https://www.youtube.com/watch?v={vid}",
            author_name=html.unescape(sn.get("channelTitle", "")), author_handle="YouTube",
            author_keys=[f"youtube:{chan}".lower(), chan.lower()] if chan else [], avatar=avatars.get(chan, ""),
            text=title, created=_iso(sn.get("publishedAt")), image=thumb, image_alt=f"Video thumbnail: {title}",
            video=True, live=sn.get("liveBroadcastContent") == "live", rev=_rev(title),
        ))
    return out


async def _get_bytes(s: aiohttp.ClientSession, url: str, prev_meta: dict[str, Any]) -> tuple[bytes | None, dict]:
    """GET with If-None-Match/If-Modified-Since. Returns (None, meta) when the feed hasn't changed."""
    headers = {}
    if prev_meta.get("url") == url:
        if prev_meta.get("etag"):
            headers["If-None-Match"] = prev_meta["etag"]
        if prev_meta.get("last_modified"):
            headers["If-Modified-Since"] = prev_meta["last_modified"]
    async with s.get(url, headers=headers) as r:
        if r.status == 304:
            return None, prev_meta
        if r.status == 404:
            raise SourceError("feed not found (HTTP 404)")
        r.raise_for_status()
        meta = {"url": url, "etag": r.headers.get("ETag", ""), "last_modified": r.headers.get("Last-Modified", "")}
        return await r.read(), meta


async def _youtube_channel_id(s: aiohttp.ClientSession, channel: str) -> str:
    if re.fullmatch(r"UC[\w-]{22}", channel):
        return channel
    if not channel.startswith("@"):
        raise SourceError("channel should be a UC… channel id or an @handle")
    # Standard RSS autodiscovery: the channel page links its own feed.
    async with s.get(f"https://www.youtube.com/{quote(channel)}", headers={"Accept-Language": "en"}) as r:
        if r.status == 404:
            raise SourceError(f"no YouTube channel {channel}")
        r.raise_for_status()
        page = await r.text()
    m = re.search(r'<link rel="alternate" type="application/rss\+xml"[^>]*channel_id=(UC[\w-]{22})', page)
    if not m:
        raise SourceError(f"couldn't find the feed for {channel}; use its UC… channel id instead")
    return m.group(1)


async def fetch_rss(s: aiohttp.ClientSession, cfg: Config, src: Source, prev: dict[str, Any]) -> tuple[list[Post] | None, dict]:
    body, meta = await _get_bytes(s, src.params["url"], prev.get("meta") or {})
    return (None if body is None else await asyncio.to_thread(parse_rss, body, src)), meta


def youtube_feed_url(channel_id: str, shorts: bool) -> str:
    """The channel's uploads feed. UULF… is its long-videos playlist: the same feed without Shorts."""
    if shorts:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    return f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{channel_id[2:]}"


async def fetch_youtube(s: aiohttp.ClientSession, cfg: Config, src: Source, prev: dict[str, Any]) -> tuple[list[Post] | None, dict]:
    meta = dict(prev.get("meta") or {})
    if meta.get("channel") != src.params["channel"] or not meta.get("channel_id"):
        meta = {"channel": src.params["channel"], "channel_id": await _youtube_channel_id(s, src.params["channel"])}
    cid = meta["channel_id"]
    body, http_meta = await _get_bytes(s, youtube_feed_url(cid, bool(src.params.get("shorts"))), meta.get("http") or {})
    meta["http"] = http_meta
    return (None if body is None else await asyncio.to_thread(parse_youtube_rss, body, src)), meta


async def _yt_api(s: aiohttp.ClientSession, path: str, params: dict[str, Any], key: str) -> dict[str, Any]:
    async with s.get(f"{YT_API}/{path}", params={**params, "key": key}) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            reason = ((body.get("error") or {}).get("errors") or [{}])[0].get("reason", "")
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise SourceError("YouTube API daily quota used up; it resets at midnight Pacific time")
            if reason in ("keyInvalid", "badRequest") or r.status == 400:
                raise SourceError("YouTube rejected the API key (check the Keychain item)")
            raise SourceError(f"YouTube API: {(body.get('error') or {}).get('message', f'HTTP {r.status}')}")
        return body


async def fetch_youtube_search(s: aiohttp.ClientSession, cfg: Config, src: Source, prev: dict[str, Any]) -> tuple[list[Post], dict]:
    cred = await asyncio.to_thread(keychain_get, KC_YOUTUBE)
    if not cred:
        raise SourceError(f"no YouTube API key in the Keychain (service '{KC_YOUTUBE}') — see README")
    key = cred[1]
    params = {"part": "snippet", "type": "video", "order": "date", "maxResults": min(max(src.count, 1), 50),
              "q": src.params["query"], "safeSearch": "strict"}
    if src.params.get("channel"):
        params["channelId"] = src.params["channel"]
    if src.params.get("live"):
        params["eventType"] = "live"
    data = await _yt_api(s, "search", params, key)  # 100 quota units
    meta = dict(prev.get("meta") or {})
    avatars: dict[str, str] = dict(meta.get("avatars") or {})
    missing = sorted({(it.get("snippet") or {}).get("channelId") for it in data.get("items", [])} - set(avatars) - {None})
    if missing and time.time() - meta.get("avatars_at", 0) > 3600:
        ch = await _yt_api(s, "channels", {"part": "snippet", "id": ",".join(missing[:50])}, key)  # 1 unit
        for c in ch.get("items", []):
            avatars[c["id"]] = ((c.get("snippet") or {}).get("thumbnails") or {}).get("default", {}).get("url", "")
        meta.update(avatars=avatars, avatars_at=time.time())
    return parse_youtube_search(data, src, avatars), meta


# ---------------------------------------------------------------- X (curated links via the embed endpoint)


def parse_x_oembed(data: dict[str, Any], url: str, source: str = "") -> Post:
    raw = data.get("html", "")
    m = re.search(r"<p[^>]*>(.*?)</p>", raw, re.S)
    inner = m.group(1) if m else ""
    # Photo links (pic.twitter.com/…) can't be shown from the embed; flag them instead.
    hidden = bool(re.search(r">pic\.(?:twitter|x)\.com/", inner))
    inner = re.sub(r'<a [^>]*>pic\.(?:twitter|x)\.com/[^<]*</a>', "", inner)
    text = html_to_text(inner)
    dm = re.findall(r"<a [^>]*>([A-Z][a-z]+ \d{1,2}, \d{4})</a>", raw)
    created = ""
    if dm:
        with contextlib.suppress(ValueError):
            created = datetime.strptime(dm[-1], "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    handle = (data.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1]
    status_id = re.search(r"/status/(\d+)", url)
    return Post(
        id=f"x:{status_id.group(1) if status_id else url}", platform="x", source=source, url=url,
        author_name=data.get("author_name", "") or handle, author_handle=f"@{handle}" if handle else "",
        author_keys=[f"x:{handle}".lower()] if handle else [], text=text, created=created, day_only=True,
        has_hidden_media=hidden, rev=_rev(text),
    )


async def fetch_x(s: aiohttp.ClientSession, cfg: Config, src: Source, prev: dict[str, Any]) -> tuple[list[Post], dict]:
    old = {p["url"]: p for p in prev.get("posts") or []}
    urls = list(src.params["urls"])[: max(src.count, len(src.params["urls"]))]

    async def one(url: str) -> Post | None:
        try:
            async with s.get(X_OEMBED, params={"url": url, "omit_script": "1", "dnt": "true"}) as r:
                if r.status == 404:
                    return None  # deleted, protected or suspended: off the wall
                r.raise_for_status()
                return parse_x_oembed(await r.json(content_type=None), url, src.id)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            if url in old:  # keep the last good copy of this one post
                log.warning("x: %s failed (%s), keeping the last good copy", url, e)
                return Post.from_dict(old[url])
            raise SourceError(f"X embed failed for {url}") from e

    res = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    posts = [r for r in res if isinstance(r, Post)]
    errs = [r for r in res if isinstance(r, BaseException)]
    if errs and not posts:
        raise errs[0]
    return posts, {}


FETCHERS = {
    "rss": fetch_rss, "youtube": fetch_youtube, "youtube_search": fetch_youtube_search,
    "bluesky_author": fetch_bluesky, "bluesky_feed": fetch_bluesky, "bluesky_search": fetch_bluesky,
    "bluesky_timeline": fetch_bluesky, "mastodon_account": fetch_mastodon, "mastodon_tag": fetch_mastodon,
    "x_posts": fetch_x,
}


def reason(e: BaseException) -> str:
    if isinstance(e, SourceError):
        return str(e)
    if isinstance(e, asyncio.TimeoutError | TimeoutError):
        return "timed out"
    if isinstance(e, aiohttp.ClientResponseError):
        return f"HTTP {e.status}"
    if isinstance(e, aiohttp.ClientConnectorError):
        return "couldn't connect"
    if isinstance(e, aiohttp.ClientError):
        return f"network error ({e.__class__.__name__})"
    return str(e) or e.__class__.__name__


def session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA})
