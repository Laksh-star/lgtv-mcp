"""Decide, post by post, whether it goes on the wall, waits for approval, or is dropped.

Rules that always apply (both profiles): public posts only; posts carrying an adult, violent or
"hide" label are dropped, as are posts whose author asked not to be shown to logged-out viewers
(Bluesky's !no-unauthenticated); blocked accounts are dropped, including when someone else
reposts or quotes them; the keyword/regex blocklist is checked against the text, names, alt text,
link previews and content warnings.

Profile rules: replies, reposts, bots, sensitive posts, Mastodon accounts that opted out of
discovery, the account allowlist for open sources (hashtags, searches, custom feeds), and the
approval queue. Curated X links skip the allowlist and the queue: you chose each one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .config import Profile, Source
from .sources import Post

DROP_LABELS = {
    "porn", "sexual", "nudity", "graphic-media", "gore", "nsfl", "sexual-figurative",
    "!hide", "!warn", "!takedown", "!no-unauthenticated",
    "spam", "scam", "impersonation", "intolerant", "threat", "self-harm", "rude",
}
CURATED_KINDS = {"x_posts"}


@dataclass(frozen=True)
class Verdict:
    action: str          # show | pending | drop
    reason: str = ""


def norm_account(entry: str) -> str:
    """'@Org.bsky.social' -> 'org.bsky.social'; '@me@mastodon.social' -> 'me@mastodon.social';
    'https://bsky.app/profile/x.com' -> 'x.com'; 'x:@jack' -> 'x:jack'."""
    e = entry.strip().lower()
    e = re.sub(r"^https?://bsky\.app/profile/", "", e).rstrip("/")
    m = re.match(r"^https?://([^/]+)/@([^/]+)$", e)  # a Mastodon profile link
    if m:
        e = f"{m.group(2)}@{m.group(1)}"
    m = re.match(r"^https?://(?:www\.)?(?:x|twitter)\.com/([a-z0-9_]+)$", e)
    if m:
        e = f"x:{m.group(1)}"
    if e.startswith("x:@") or e.startswith("youtube:@"):
        e = e.replace(":@", ":", 1)
    return e.removeprefix("@")


def _matches(keys: list[str], entries: frozenset[str]) -> bool:
    return any(k in entries for k in keys)


@lru_cache(maxsize=64)
def _accounts(entries: tuple[str, ...]) -> frozenset[str]:
    return frozenset(norm_account(e) for e in entries if e.strip())


@lru_cache(maxsize=64)
def _block_regexes(words: tuple[str, ...], patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    # Each pattern is compiled on its own, so inline flags like (?i) keep working.
    rx = [re.compile("|".join(rf"(?<!\w){re.escape(w.strip())}(?!\w)" for w in words if w.strip()), re.I)] if any(
        w.strip() for w in words) else []
    return tuple(rx + [re.compile(p) for p in patterns if p])


def blocked_text(post: Post, profile: Profile) -> str | None:
    for rx in _block_regexes(tuple(profile.block_words), tuple(profile.block_patterns)):
        for value in (post.text, post.author_name, post.author_handle, post.image_alt, post.card_title,
                      post.content_warning):
            m = rx.search(value or "")
            if m:
                return m.group(0)
    return None


def moderate(post: Post, profile: Profile, source: Source, decisions: dict[str, Any]) -> Verdict:
    if post.visibility != "public":
        return Verdict("drop", f"not public ({post.visibility})")
    bad = sorted(set(post.labels) & DROP_LABELS)
    if bad:
        return Verdict("drop", f"label {bad[0]}")

    blocked = _accounts(tuple(profile.block_accounts))
    if _matches(post.author_keys, blocked):
        return Verdict("drop", "blocked account")
    if _matches(post.reposted_by_keys, blocked) or _matches(post.quoted_keys, blocked):
        return Verdict("drop", "repost or quote involving a blocked account")
    if word := blocked_text(post, profile):
        return Verdict("drop", f"blocked word '{word}'")

    if post.is_reply and not profile.include_replies:
        return Verdict("drop", "reply")
    if post.reposted_by and not profile.include_reposts:
        return Verdict("drop", "repost")
    if post.bot and profile.drop_bots:
        return Verdict("drop", "bot account")
    if (post.sensitive or post.content_warning) and profile.drop_sensitive:
        return Verdict("drop", "marked sensitive")
    if source.open and profile.respect_discovery and not post.discoverable:
        return Verdict("drop", "account opted out of discovery")

    curated = source.kind in CURATED_KINDS
    if source.open and not curated:
        allowed = _accounts(tuple(profile.allow_accounts))
        if allowed and not _matches(post.author_keys, allowed):
            return Verdict("drop", "not on the allowlist")
        if not allowed and profile.require_allowlist:
            return Verdict("drop", "open source and the allowlist is empty")

    if profile.approval and not curated:
        d = decisions.get(post.id)
        if d is None:
            return Verdict("pending", "waiting for approval")
        if d.get("status") == "rejected":
            return Verdict("drop", "rejected")
        if d.get("rev") != post.rev:
            return Verdict("pending", "edited since it was approved")
    return Verdict("show")
