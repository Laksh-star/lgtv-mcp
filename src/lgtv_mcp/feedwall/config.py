"""Feed wall settings (~/.config/lgtv/feedwall.toml), sources added from chat, and Keychain secrets.

Sources come from two places: the [[sources]] tables in feedwall.toml (edited by hand), and
feedwall-sources.json, which manage_feed_source writes so the TOML file and its comments are never
rewritten. A source removed from chat that lives in the TOML file is listed there as disabled.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tv import CONFIG_DIR
from .store import locked_json

CONFIG_FILE = CONFIG_DIR / "feedwall.toml"
OVERLAY_FILE = CONFIG_DIR / "feedwall-sources.json"

# Keychain items (login keychain). Values never go in files or the repo.
KC_BLUESKY = "lgtv-feedwall-bluesky"            # account = your handle, password = an app password
KC_BLUESKY_SESSION = "lgtv-feedwall-bluesky-session"  # saved login tokens (createSession is limited to ~10/day)
KC_YOUTUBE = "lgtv-feedwall-youtube"            # optional YouTube Data API key

KINDS = {
    "rss": ("url",),
    "youtube": ("channel",),               # UC… channel id or @handle; RSS, no key
    "youtube_search": ("query",),          # YouTube Data API (needs the key); optional channel, live
    "bluesky_author": ("actor",),
    "bluesky_feed": ("feed",),             # a custom feed: at://… or a bsky.app/profile/…/feed/… link
    "bluesky_search": ("query",),          # needs your app password
    "bluesky_timeline": (),                # your home timeline; needs your app password
    "mastodon_account": ("instance", "account"),
    "mastodon_tag": ("instance", "tag"),
    "x_posts": ("urls",),                  # curated post links, through X's embed endpoint
}
# Sources where anyone can appear: the account allowlist applies to these.
OPEN_KINDS = {"bluesky_search", "bluesky_feed", "mastodon_tag", "youtube_search"}
# Never fetch these more often than this (YouTube's feed is cached for 15 min; the search API has a quota).
MIN_REFRESH_MINUTES = {"youtube": 15, "youtube_search": 15}

DEFAULT_TOML = """\
# Feed wall settings. After changing a profile's days/hours, run:  uv run feedwall install
# Everything else is picked up on the wall's next refresh.

[wall]
rotate_seconds = 12      # time on each post
refresh_minutes = 3      # how often sources are fetched again
max_posts = 30           # newest posts kept on the wall

[bluesky]
login_service = "https://bsky.social"   # where your account lives (your PDS)
# The app password is read from the Keychain (service "lgtv-feedwall-bluesky", account = your handle).

[profiles.personal]
mode = "personal"        # your own sources; never takes over a busy TV (a toast instead)
title = "Feed wall"
sources = ["nasa-youtube", "bluesky-team"]

[profiles.organization]
mode = "organization"    # strict moderation: allowlist for open sources, approval queue, no reposts
title = "#YourEvent"
sources = []
days = [1, 2, 3, 4, 5]   # 1 = Monday ... 7 = Sunday
hours = ""               # e.g. "09:00-18:00"; empty = only when started from chat
at_end = "home"          # "home" or "off" when the hours end
allow_accounts = []      # e.g. ["@org.bsky.social", "@speaker@mastodon.social", "youtube:UC…"]
block_words = []
block_patterns = []      # regular expressions, e.g. '(?i)\\bgiveaway\\b'

[[sources]]
id = "nasa-youtube"
kind = "youtube"
channel = "@NASA"
shorts = false

[[sources]]
id = "bluesky-team"
kind = "bluesky_author"
actor = "bsky.app"
"""


@dataclass
class Source:
    id: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    count: int = 20
    origin: str = "feedwall.toml"
    profiles: list[str] = field(default_factory=list)  # only for sources added from chat

    @property
    def open(self) -> bool:
        return self.kind in OPEN_KINDS

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass
class Profile:
    name: str
    mode: str = "personal"
    title: str = "Feed wall"
    sources: list[str] = field(default_factory=list)
    take_over: bool = False
    approval: bool = False
    include_reposts: bool = True
    include_replies: bool = False
    drop_bots: bool = False
    drop_sensitive: bool = True
    require_allowlist: bool = False       # open sources show nothing unless the author is allowed
    respect_discovery: bool = False       # skip Mastodon accounts that opted out of discovery
    allow_accounts: list[str] = field(default_factory=list)
    block_accounts: list[str] = field(default_factory=list)
    block_words: list[str] = field(default_factory=list)
    block_patterns: list[str] = field(default_factory=list)
    days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    hours: str = ""
    at_end: str = "home"
    minutes: int = 60                     # default length when started from chat

    @property
    def organization(self) -> bool:
        return self.mode == "organization"


MODE_DEFAULTS = {
    "personal": dict(take_over=False, approval=False, include_reposts=True, drop_bots=False,
                     require_allowlist=False, respect_discovery=False, minutes=30),
    "organization": dict(take_over=True, approval=True, include_reposts=False, drop_bots=True,
                         require_allowlist=True, respect_discovery=True, minutes=120),
}


@dataclass
class Config:
    rotate_seconds: int = 12
    refresh_minutes: int = 3
    max_posts: int = 30
    bluesky_service: str = "https://bsky.social"
    profiles: dict[str, Profile] = field(default_factory=dict)
    sources: dict[str, Source] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)

    def profile(self, name: str) -> Profile:
        if name not in self.profiles:
            raise ValueError(f"No profile '{name}' in {CONFIG_FILE.name}; there are: {', '.join(self.profiles) or 'none'}.")
        return self.profiles[name]

    def sources_for(self, profile: Profile) -> list[Source]:
        ids = list(profile.sources) + [s.id for s in self.sources.values() if profile.name in s.profiles]
        out, seen = [], set()
        for i in ids:
            if i in seen or i in self.disabled or i not in self.sources:
                continue
            seen.add(i)
            out.append(self.sources[i])
        return out


def _source(raw: dict[str, Any], origin: str) -> Source:
    raw = dict(raw)
    sid, kind = str(raw.pop("id", "")).strip(), str(raw.pop("kind", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", sid):
        raise ValueError(f"source id '{sid}' should be letters, digits, '-', '_' or '.' (max 41)")
    if kind not in KINDS:
        raise ValueError(f"source '{sid}': unknown kind '{kind}' (one of {', '.join(KINDS)})")
    missing = [k for k in KINDS[kind] if not raw.get(k)]
    if missing:
        raise ValueError(f"source '{sid}' ({kind}) needs {', '.join(missing)}")
    return Source(id=sid, kind=kind, name=str(raw.pop("name", "")), count=int(raw.pop("count", 20)),
                  profiles=list(raw.pop("profiles", [])), origin=origin, params=raw)


def _profile(name: str, raw: dict[str, Any]) -> Profile:
    mode = str(raw.get("mode", name if name in MODE_DEFAULTS else "personal"))
    if mode not in MODE_DEFAULTS:
        raise ValueError(f"profile '{name}': mode must be 'personal' or 'organization'")
    values = {**MODE_DEFAULTS[mode], **{k: v for k, v in raw.items() if k != "mode"}}
    known = {f for f in Profile.__dataclass_fields__ if f not in ("name", "mode")}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"profile '{name}': unknown setting(s) {', '.join(sorted(unknown))}")
    p = Profile(name=name, mode=mode, **values)
    if p.at_end not in ("home", "off"):
        raise ValueError(f"profile '{name}': at_end must be 'home' or 'off'")
    for pat in p.block_patterns:
        re.compile(pat)
    from .schedule import parse_hours
    parse_hours(p.hours)
    return p


def read_overlay() -> dict[str, Any]:
    try:
        return json.loads(OVERLAY_FILE.read_text())
    except (OSError, ValueError):
        return {}


def load_config(text: str | None = None, overlay: dict[str, Any] | None = None) -> Config:
    if text is None:
        text = CONFIG_FILE.read_text() if CONFIG_FILE.exists() else DEFAULT_TOML
    raw = tomllib.loads(text)
    overlay = read_overlay() if overlay is None else overlay
    wall, bsky = raw.get("wall", {}), raw.get("bluesky", {})
    cfg = Config(
        rotate_seconds=max(4, int(wall.get("rotate_seconds", 12))),
        refresh_minutes=max(1, int(wall.get("refresh_minutes", 3))),
        max_posts=max(1, int(wall.get("max_posts", 30))),
        bluesky_service=str(bsky.get("login_service", "https://bsky.social")).rstrip("/"),
        disabled=set(overlay.get("disabled", [])),
    )
    for name, p in raw.get("profiles", {}).items():
        cfg.profiles[name] = _profile(name, p)
    for s in raw.get("sources", []):
        src = _source(s, "feedwall.toml")
        cfg.sources[src.id] = src
    for s in overlay.get("added", []):
        src = _source(s, "added from chat")
        cfg.sources.setdefault(src.id, src)
    return cfg


def ensure_config_file() -> bool:
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(DEFAULT_TOML)
    return True


# ---------------------------------------------------------------- sources added/removed from chat


def add_source(raw: dict[str, Any], profile: str) -> str:
    cfg = load_config()
    cfg.profile(profile)
    with locked_json(OVERLAY_FILE) as ov:
        added = ov.setdefault("added", [])
        if raw.get("kind") == "x_posts":
            # Post links accumulate in one curated source per profile.
            sid = raw.get("id") or f"x-posts-{profile}"
            existing = next((a for a in added if a["id"] == sid), None)
            if existing is None and sid in cfg.sources:
                raise ValueError(f"'{sid}' is defined in {CONFIG_FILE.name}; add the link there instead.")
            urls = [normalize_x_url(u) for u in raw["urls"]]
            if existing:
                existing["urls"] = list(dict.fromkeys(existing["urls"] + urls))
                if profile not in existing["profiles"]:
                    existing["profiles"].append(profile)
                return f"Added {len(urls)} X post link(s) to '{sid}' ({len(existing['urls'])} in all)."
            raw = {"id": sid, "kind": "x_posts", "name": "X posts", "urls": urls}
        if raw["id"] in cfg.sources and raw["id"] not in ov.get("disabled", []):
            raise ValueError(f"There's already a source '{raw['id']}'.")
        _source(raw, "added from chat")  # validate
        ov["added"] = [a for a in added if a["id"] != raw["id"]] + [{**raw, "profiles": [profile]}]
        ov["disabled"] = [d for d in ov.get("disabled", []) if d != raw["id"]]
    return f"Added source '{raw['id']}' ({raw['kind']}) to the {profile} profile."


def remove_source(source_id: str) -> str:
    cfg = load_config()
    src = cfg.sources.get(source_id)
    if src is None:
        raise ValueError(f"No source '{source_id}'. Known: {', '.join(cfg.sources) or 'none'}.")
    with locked_json(OVERLAY_FILE) as ov:
        if src.origin == "added from chat":
            ov["added"] = [a for a in ov.get("added", []) if a["id"] != source_id]
            return f"Removed source '{source_id}'."
        ov["disabled"] = sorted(set(ov.get("disabled", [])) | {source_id})
    return f"Turned off source '{source_id}' (it's defined in {CONFIG_FILE.name}; delete it there to remove it for good)."


def normalize_x_url(url: str) -> str:
    m = re.match(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)", url.strip())
    if not m:
        raise ValueError(f"'{url}' isn't an X post link (https://x.com/<user>/status/<id>).")
    return f"https://x.com/{m.group(1)}/status/{m.group(2)}"


# ---------------------------------------------------------------- Keychain


def keychain_get(service: str, account: str | None = None) -> tuple[str, str] | None:
    """(account, secret) for a login-keychain item, or None."""
    cmd = ["security", "find-generic-password", "-s", service] + (["-a", account] if account else [])
    r = subprocess.run([*cmd, "-g"], capture_output=True, text=True, check=False, timeout=90)
    if r.returncode:
        return None
    acct = re.search(r'"acct"<blob>="([^"]*)"', r.stdout)
    pw = re.search(r'^password: "(.*)"$', r.stderr, re.M)
    if not pw:
        return None
    return (acct.group(1) if acct else ""), pw.group(1)


def keychain_set(service: str, account: str, secret: str) -> None:
    """Store a secret without putting it on a command line (fed to `security -i` on stdin)."""
    if any(c in secret for c in '"\\\n'):
        raise ValueError("secret contains characters that can't be stored this way")
    cmd = f'add-generic-password -U -s "{service}" -a "{account}" -w "{secret}"\n'
    subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, check=True, timeout=30)


LOG_FILE = Path.home() / "Library/Logs/lg-tv-feedwall.log"


def setup_logging() -> logging.Logger:
    log = logging.getLogger("feedwall")
    if not log.handlers:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log
