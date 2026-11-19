"""Source parsing, against responses recorded on 18 Nov 2026 (names, text and links replaced)."""

import asyncio

from lgtv_mcp.feedwall import wall
from lgtv_mcp.feedwall.config import Source, load_config, normalize_x_url
from lgtv_mcp.feedwall.sources import (
    Post, html_to_text, parse_bsky_feed, parse_mastodon, parse_rss, parse_x_oembed, parse_youtube_rss,
    parse_youtube_search, youtube_feed_url,
)
from lgtv_mcp.feedwall.store import load_source


def test_bluesky_author_feed_marks_reposts_and_quotes(fixture_json):
    posts = parse_bsky_feed(fixture_json("bsky_author_feed.json"), "s")
    assert len(posts) == 5
    reposts = [p for p in posts if p.reposted_by]
    assert len(reposts) == 3
    assert all(p.reposted_by == "@user8.example" and p.reposted_by_keys for p in reposts)
    quoting = [p for p in posts if p.quoted_keys]
    assert quoting and all(k for k in quoting[0].quoted_keys)
    p = posts[0]
    assert p.id.startswith("bsky:at://") and p.url.startswith("https://bsky.app/profile/user3.example/post/")
    assert p.platform == "bluesky" and p.source == "s" and p.created.endswith("+00:00")


def test_bluesky_search_keeps_labels_alt_text_and_link_cards(fixture_json):
    posts = parse_bsky_feed(fixture_json("bsky_search.json"))
    labels = {lab for p in posts for lab in p.labels}
    assert {"porn", "nudity", "bot", "!no-unauthenticated"} <= labels
    assert any(p.bot for p in posts)
    assert any(p.is_reply for p in posts)
    with_alt = [p for p in posts if p.image and p.image_alt]
    assert with_alt and with_alt[0].image.startswith("https://")
    assert any(p.card_title for p in posts)  # external link preview
    # the gallery embed (a newer embed type) still yields its first image
    assert posts[-1].image and posts[-1].image_alt


def test_mastodon_account_reblogs_show_the_original(fixture_json):
    posts = parse_mastodon(fixture_json("mastodon_account.json"), "host.example", "m")
    assert len(posts) == 5
    reblogs = [p for p in posts if p.reposted_by]
    assert len(reblogs) == 4
    assert all(p.author_handle != p.reposted_by or p.author_keys for p in reblogs)
    videos = [p for p in posts if p.video]
    assert videos and videos[0].image  # a still, never the video itself
    assert posts[0].text == "Sample content 4\nsecond line & more"  # HTML turned into text


def test_mastodon_tag_media_alt_and_local_accounts(fixture_json):
    posts = parse_mastodon(fixture_json("mastodon_tag.json"), "host.example")
    assert all(p.visibility == "public" for p in posts)
    assert all("@" in p.author_handle[1:] for p in posts)  # always user@instance
    assert sum(bool(p.image_alt) for p in posts) == 3
    assert posts[0].rev and posts[0].rev != posts[1].rev


def test_mastodon_edit_changes_rev(fixture_json):
    st = fixture_json("mastodon_tag.json")[0]
    before = parse_mastodon([st], "h")[0]
    edited = dict(st, content="<p>changed</p>", edited_at="2026-11-18T14:00:00.000Z")
    after = parse_mastodon([edited], "h")[0]
    assert before.id == after.id and before.rev != after.rev and after.text == "changed"


def test_mastodon_private_reblog_wrapper_is_not_public(fixture_json):
    st = dict(fixture_json("mastodon_account.json")[1], visibility="private")
    assert parse_mastodon([st], "h")[0].visibility == "private"


def test_rss_and_atom(fixture_bytes):
    rss = parse_rss(fixture_bytes("rss.xml"), Source("news", "rss", {"url": "https://media.example/x"}, count=2))
    assert len(rss) == 2  # count is respected
    assert rss[0].image.endswith(".jpg") and rss[0].text.startswith("Sample title 3\n\nSample description")
    assert rss[0].created == "2026-11-18T12:39:24+00:00"
    atom = parse_rss(fixture_bytes("atom.xml"), Source("blog", "rss", {"url": "https://media.example/y"}))
    assert len(atom) == 2 and atom[0].url.startswith("https://") and not atom[0].image


def test_youtube_rss(fixture_bytes):
    posts = parse_youtube_rss(fixture_bytes("youtube.xml"), Source("yt", "youtube", {"channel": "@x"}))
    assert len(posts) == 6
    p = posts[0]
    assert p.id == "yt:vid00000054" and p.video and p.image.endswith(".jpg")
    assert p.author_keys[0] == "youtube:ucfixturefixturefixture0"
    assert p.image_alt.startswith("Video thumbnail:")


def test_youtube_feed_url_drops_shorts_by_default():
    cid = "UCLA_DiR1FfKNvjuUpBHmylQ"
    assert youtube_feed_url(cid, False).endswith("playlist_id=UULFLA_DiR1FfKNvjuUpBHmylQ")
    assert youtube_feed_url(cid, True).endswith(f"channel_id={cid}")


def test_youtube_search_api_shape():
    # Shape from the YouTube Data API v3 search.list docs (not recorded: no API key on this Mac yet).
    data = {"items": [
        {"id": {"kind": "youtube#video", "videoId": "abc123"}, "snippet": {
            "publishedAt": "2026-11-18T10:00:00Z", "channelId": "UCfixturefixturefixture0", "title": "Keynote &amp; Q&amp;A",
            "channelTitle": "Example Org", "liveBroadcastContent": "live",
            "thumbnails": {"high": {"url": "https://media.example/t.jpg"}}}},
        {"id": {"kind": "youtube#channel", "channelId": "UCx"}, "snippet": {"title": "a channel"}},
    ]}
    posts = parse_youtube_search(data, Source("ys", "youtube_search", {"query": "#x"}), {"UCfixturefixturefixture0": "https://media.example/a.jpg"})
    assert len(posts) == 1
    p = posts[0]
    assert p.text == "Keynote & Q&A" and p.live and p.video and p.avatar.endswith("a.jpg")
    assert p.url == "https://www.youtube.com/watch?v=abc123"


def test_x_oembed():
    # Shape as returned by publish.x.com/oembed on 18 Nov 2026, with made-up content.
    data = {"url": "https://x.com/example/status/123", "author_name": "Example Person",
            "author_url": "https://x.com/example",
            "html": '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">Hello &amp; welcome<br>to the event '
                    '<a href="https://t.co/abc">pic.twitter.com/abc</a></p>&mdash; Example Person (@example) '
                    '<a href="https://x.com/example/status/123?ref_src=twsrc%5Etfw">March 21, 2006</a></blockquote>\n'}
    p = parse_x_oembed(data, "https://x.com/example/status/123", "x")
    assert p.id == "x:123" and p.author_handle == "@example" and p.author_keys == ["x:example"]
    assert p.text == "Hello & welcome\nto the event" and p.has_hidden_media
    assert p.created.startswith("2006-03-21") and p.day_only


def test_normalize_x_url():
    assert normalize_x_url("https://twitter.com/Jack/status/20?s=1") == "https://x.com/Jack/status/20"
    assert normalize_x_url("https://mobile.x.com/a_b/status/99") == "https://x.com/a_b/status/99"


def test_html_to_text():
    assert html_to_text("<p>a<br/>b</p><p>c &lt;3</p>") == "a\nb\n\nc <3"


TOML = """
[profiles.personal]
sources = ["good", "dead"]
[[sources]]
id = "good"
kind = "rss"
url = "https://media.example/good"
[[sources]]
id = "dead"
kind = "rss"
url = "https://media.example/dead"
"""


def test_a_dead_source_keeps_its_last_good_posts(monkeypatch):
    cfg = load_config(TOML, overlay={})
    profile = cfg.profile("personal")
    calls = {"dead": 0}

    async def fake_fetch(s, cfg, src, prev):
        if src.id == "dead":
            calls["dead"] += 1
            if calls["dead"] > 1:
                raise TimeoutError()
        return [Post(id=f"rss:{src.id}", platform="rss", source=src.id, text=src.id, created="2026-11-18T10:00:00+00:00")], {}

    monkeypatch.setattr(wall, "FETCHERS", {"rss": fake_fetch})
    asyncio.run(wall.refresh(cfg, profile, force=True))
    asyncio.run(wall.refresh(cfg, profile, force=True))  # now "dead" fails
    entry = load_source("dead")
    assert entry["error"] == "timed out" and entry["posts"] and entry["ok_at"] < entry["tried_at"]
    w = wall.build(cfg, profile, decisions={})
    assert {p.source for p in w.shown} == {"good", "dead"}
    dead = next(s for s in w.sources if s["id"] == "dead")
    assert not dead["ok"] and dead["last_updated"]


def test_refresh_respects_the_interval(monkeypatch):
    cfg = load_config(TOML, overlay={})
    n = {"calls": 0}

    async def fake_fetch(s, cfg, src, prev):
        n["calls"] += 1
        return [], {}

    monkeypatch.setattr(wall, "FETCHERS", {"rss": fake_fetch})
    asyncio.run(wall.refresh(cfg, cfg.profile("personal")))
    asyncio.run(wall.refresh(cfg, cfg.profile("personal")))
    assert n["calls"] == 2  # two sources, fetched once each


def test_interleave_mixes_sources():
    posts = [Post(id=f"a{i}", platform="rss", source="busy", created=f"2026-11-18T10:{i:02d}:00") for i in range(10)]
    posts += [Post(id="b", platform="rss", source="quiet", created="2026-11-17T10:00:00")]
    out = wall.interleave(posts, 4)
    assert [p.source for p in out][:2] == ["busy", "quiet"] and len(out) == 4
