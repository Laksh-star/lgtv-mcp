"""Turn recorded API responses into committable fixtures.

The structure (keys, embed types, labels, visibility, reposts, replies, dates) is kept exactly as
recorded; every name, handle, id, text and link is replaced with a made-up value, because the
recordings are strangers' posts and this repository is public.

    python -I tests/fixtures/scrub.py <recordings dir> tests/fixtures
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TEXT_KEYS = {"text", "content", "alt", "description", "title", "displayName", "display_name", "note",
             "spoiler_text", "name", "summary", "avatar_description", "header_description", "author_name"}
ID_KEYS = {"handle", "acct", "username"}
DROP_KEYS = {"fields", "emojis", "facets", "mentions", "tags", "card", "application", "associated", "viewer",
             "pinnedPost", "threadgate", "langs", "tagged_collections", "roles", "featured_tags"}


class Scrubber:
    def __init__(self):
        self.map: dict[str, str] = {}

    def _m(self, original: str, fmt: str) -> str:
        if original not in self.map:
            self.map[original] = fmt.format(n=len(self.map) + 1)
        return self.map[original]

    def did(self, s: str) -> str:
        return re.sub(r"did:plc:[a-z0-9]+", lambda m: self._m(m.group(0), "did:plc:fixture{n:04d}"), s)

    def url(self, s: str) -> str:
        ext = re.search(r"\.(jpe?g|png|webp|gif|mp4)(?:$|\?)", s)
        return self._m(s, "https://media.example/f{n}" + (f".{ext.group(1)}" if ext else ""))

    def handle(self, s: str) -> str:
        if "@" in s:
            user, host = s.split("@", 1)
            return f"{self._m(user, 'user{n}')}@{self._m(host, 'host{n}.example')}"
        return s if s == "handle.invalid" else self._m(s, "user{n}.example")

    def text(self, key: str, s: str) -> str:
        if not s:
            return s
        tags = " ".join(sorted(set(re.findall(r"#photography|#bluesky", s, re.I)))).lower()
        body = f"Sample {key} {len(self.map) + 1}"
        self.map[f"text:{len(self.map)}"] = body
        if key in ("content",):
            return f"<p>{body}{' ' + tags if tags else ''}<br>second line &amp; more</p>"
        return body + (f" {tags}" if tags else "")

    def walk(self, v, key=""):
        if isinstance(v, dict):
            return {k: self.walk(x, k) for k, x in v.items() if k not in DROP_KEYS}
        if isinstance(v, list):
            return [self.walk(x, key) for x in v]
        if not isinstance(v, str):
            return v
        if key in ID_KEYS:
            return self.handle(v)
        if key in TEXT_KEYS:
            return self.text(key, v)
        if key in ("cid", "id") and not v.isdigit():
            return self._m(v, "fixture-{n}")
        if v.startswith("at://"):
            return self.did(v)
        if v.startswith("did:"):
            return self.did(v)
        if v.startswith("http"):
            return self.url(v)
        return v


def scrub_json(src: Path, dst: Path, pick) -> None:
    data = json.loads(src.read_text())
    dst.write_text(json.dumps(Scrubber().walk(pick(data)), indent=1, ensure_ascii=False) + "\n")


def scrub_xml(src: Path, dst: Path, items: int) -> None:
    s = src.read_text()
    tag = "item" if "<item>" in s else "entry"
    head, *rest = re.split(rf"(?=<{tag}[ >])", s)
    tail = rest[-1][rest[-1].rfind(f"</{tag}>") + len(tag) + 3:] if rest else ""
    body = "".join(r[: r.rfind(f"</{tag}>") + len(tag) + 3] for r in rest[:items])
    s = head + body + tail
    sc = Scrubber()
    keep = re.findall(r'xmlns(?::\w+)?="[^"]*"', s)
    for i, k in enumerate(keep):
        s = s.replace(k, f"@@NS{i}@@", 1)
    for t in ("title", "description", "media:title", "media:description", "name", "dc:creator", "author", "summary",
              "content", "copyright", "uri"):
        s = re.sub(rf"(<{t}(?: [^>]*)?>)(.*?)(</{t}>)", lambda m, t=t: m.group(1) + sc.text(t, m.group(2)) + m.group(3), s, flags=re.S)
    s = re.sub(r"https?://[^\s\"<>']+", lambda m: sc.url(m.group(0)), s)
    s = re.sub(r"(<yt:videoId>)([^<]+)", lambda m: m.group(1) + sc._m(m.group(2), "vid{n:08d}"), s)
    s = re.sub(r"(<yt:channelId>)([^<]+)", lambda m: m.group(1) + "UCfixturefixturefixture0", s)
    s = re.sub(r"(<id>|<guid[^>]*>)([^<]+)", lambda m: m.group(1) + sc._m(m.group(2), "fixture-id-{n}"), s)
    for i, k in enumerate(keep):
        s = s.replace(f"@@NS{i}@@", k, 1)
    dst.write_text(s)


def main(rec: str, out: str) -> None:
    r, o = Path(rec), Path(out)
    o.mkdir(parents=True, exist_ok=True)
    scrub_json(r / "b1.body", o / "bsky_author_feed.json", lambda d: {"feed": d["feed"][:5], "cursor": "fixture"})
    scrub_json(r / "b11.body", o / "bsky_search.json", lambda d: {"posts": [d["posts"][i] for i in (0, 1, 3, 4, 12, 14, 20, 24)]})
    scrub_json(r / "m2_mastodon.social.body", o / "mastodon_account.json", lambda d: d[:5])
    scrub_json(r / "m3_mastodon.social.body", o / "mastodon_tag.json", lambda d: d[:4])
    scrub_xml(r / "bbc.body", o / "rss.xml", 3)
    scrub_xml(r / "sw.body", o / "atom.xml", 2)
    scrub_xml(r / "yt1.body", o / "youtube.xml", 6)


if __name__ == "__main__":
    main(*sys.argv[1:3])
