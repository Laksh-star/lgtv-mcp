"""Moderation: always-on rules, allowlist, blocklist and the approval queue."""

import pytest

from lgtv_mcp.feedwall import wall
from lgtv_mcp.feedwall.config import Profile, Source, _profile, load_config
from lgtv_mcp.feedwall.moderation import moderate, norm_account
from lgtv_mcp.feedwall.sources import Post, parse_bsky_feed

OPEN = Source("tag", "mastodon_tag", {"instance": "h", "tag": "t"})
CLOSED = Source("acct", "bluesky_author", {"actor": "a"})
X = Source("x", "x_posts", {"urls": ["https://x.com/a/status/1"]})


def post(**kw) -> Post:
    base = dict(id="masto:1", platform="mastodon", author_name="Ann", author_handle="@ann@h.example",
                author_keys=["ann@h.example"], text="Hello #event", rev="r1")
    return Post(**{**base, **kw})


def personal(**kw) -> Profile:
    return _profile("personal", kw)


def org(**kw) -> Profile:
    return _profile("organization", {"allow_accounts": ["@ann@h.example"], **kw})


def v(p, prof, src=OPEN, decisions=None):
    return moderate(p, prof, src, decisions or {})


# ---------------------------------------------------------------- always on


@pytest.mark.parametrize("vis", ["private", "direct", "unlisted"])
def test_only_public_posts(vis):
    assert v(post(visibility=vis), personal()).action == "drop"


@pytest.mark.parametrize("label", ["porn", "nudity", "graphic-media", "!hide", "!no-unauthenticated"])
def test_drop_labels_in_every_profile(label):
    assert v(post(labels=[label]), personal(), CLOSED).action == "drop"
    assert v(post(labels=[label]), org(), CLOSED).action == "drop"


def test_harmless_labels_pass():
    assert v(post(labels=["bridged-from-bridgy-fed-activitypub"]), personal()).action == "show"


def test_repost_or_quote_of_blocked_account_is_dropped_even_with_reposts_on():
    prof = personal(block_accounts=["@troll.example"])
    assert v(post(reposted_by="@troll.example", reposted_by_keys=["troll.example"]), prof).reason.startswith("repost")
    assert v(post(quoted_keys=["troll.example"]), prof).action == "drop"
    assert v(post(author_keys=["troll.example"]), prof).reason == "blocked account"


def test_blocklist_words_and_patterns():
    prof = personal(block_words=["giveaway"], block_patterns=[r"(?i)\bcrypto\s*airdrop\b"])
    assert v(post(text="Huge GIVEAWAY today"), prof).action == "drop"
    assert v(post(text="giveaways"), prof).action == "show"  # whole words only
    assert v(post(text="free Crypto Airdrop"), prof).action == "drop"
    assert v(post(image_alt="a giveaway poster"), prof).action == "drop"  # alt text is checked too
    assert v(post(author_name="Giveaway Bot"), prof).action == "drop"
    assert v(post(card_title="Giveaway inside"), prof).action == "drop"


def test_bad_regex_is_rejected_at_load():
    with pytest.raises(Exception):
        _profile("personal", {"block_patterns": ["("]})


# ---------------------------------------------------------------- profile rules


def test_personal_defaults_allow_reposts_but_not_replies_or_sensitive():
    assert v(post(reposted_by="@b", reposted_by_keys=["b"]), personal()).action == "show"
    assert v(post(is_reply=True), personal()).action == "drop"
    assert v(post(sensitive=True), personal()).action == "drop"
    assert v(post(content_warning="spoilers"), personal()).action == "drop"


def test_organization_drops_reposts_and_bots():
    d = {"masto:1": {"status": "approved", "rev": "r1"}}
    assert v(post(reposted_by="@b", reposted_by_keys=["b"]), org(), decisions=d).reason == "repost"
    assert v(post(bot=True), org(), decisions=d).reason == "bot account"


def test_allowlist_applies_to_open_sources_only():
    prof = personal(allow_accounts=["@ann@h.example"])
    assert v(post(author_keys=["bob@h.example"]), prof, OPEN).reason == "not on the allowlist"
    assert v(post(author_keys=["bob@h.example"]), prof, CLOSED).action == "show"
    assert v(post(), prof, OPEN).action == "show"


def test_organization_with_empty_allowlist_shows_nothing_from_open_sources():
    prof = _profile("organization", {})
    assert v(post(), prof, OPEN).reason == "open source and the allowlist is empty"


def test_personal_with_empty_allowlist_allows_open_sources():
    assert v(post(), personal(), OPEN).action == "show"


def test_organization_respects_discovery_opt_out():
    d = {"masto:1": {"status": "approved", "rev": "r1"}}
    assert v(post(discoverable=False), org(), OPEN, d).reason == "account opted out of discovery"
    assert v(post(discoverable=False), org(), CLOSED, d).action == "show"


@pytest.mark.parametrize("entry,expected", [
    ("@Org.bsky.social", "org.bsky.social"), ("@me@Mastodon.Social", "me@mastodon.social"),
    ("https://bsky.app/profile/x.com", "x.com"), ("https://host.example/@me", "me@host.example"),
    ("x:@Jack", "x:jack"), ("https://x.com/Jack", "x:jack"), ("youtube:UCabc", "youtube:ucabc"),
])
def test_norm_account(entry, expected):
    assert norm_account(entry) == expected


# ---------------------------------------------------------------- approval queue


def test_new_posts_wait_for_approval():
    assert v(post(), org()).action == "pending"


def test_approved_post_shows_and_edit_sends_it_back():
    d = {"masto:1": {"status": "approved", "rev": "r1"}}
    assert v(post(), org(), decisions=d).action == "show"
    again = v(post(rev="r2"), org(), decisions=d)
    assert again.action == "pending" and "edited" in again.reason


def test_rejected_post_is_dropped():
    assert v(post(), org(), decisions={"masto:1": {"status": "rejected", "rev": "r1"}}).reason == "rejected"


def test_curated_x_links_skip_allowlist_and_queue_but_not_blocklist():
    prof = org(block_words=["spoiler"])
    xp = post(id="x:1", platform="x", author_keys=["x:someone"])
    assert v(xp, prof, X).action == "show"
    assert v(post(id="x:2", platform="x", text="big spoiler"), prof, X).action == "drop"


TOML = """
[profiles.organization]
mode = "organization"
sources = ["tag"]
allow_accounts = ["@ann@h.example"]
[[sources]]
id = "tag"
kind = "mastodon_tag"
instance = "h.example"
tag = "event"
"""


def _seed(posts):
    from lgtv_mcp.feedwall.store import save_source
    save_source("tag", {"posts": [p.to_dict() for p in posts], "ok_at": 1, "tried_at": 1})


def test_approve_newest_and_by_id_end_to_end():
    cfg = load_config(TOML, overlay={})
    prof = cfg.profile("organization")
    _seed([post(id=f"masto:{i}", created=f"2026-11-18T10:0{i}:00+00:00", rev=f"r{i}") for i in range(6)])
    w = wall.build(cfg, prof)
    assert len(w.pending) == 6 and not w.shown
    done, unknown = wall.decide(w, "approved", None, 5)  # "approve the last 5"
    assert len(done) == 5 and not unknown
    w = wall.build(cfg, prof)
    assert len(w.shown) == 5 and len(w.pending) == 1
    assert {p.id for p in w.shown} == {f"masto:{i}" for i in range(1, 6)}  # the five newest
    last = wall.short_id(w.pending[0].id)
    done, unknown = wall.decide(w, "rejected", [last, "nope00"], None)
    assert done == [last] and unknown == ["nope00"]
    w = wall.build(cfg, prof)
    assert not w.pending and w.dropped == {"rejected": 1}


def test_reject_takes_a_post_off_the_wall():
    cfg = load_config(TOML, overlay={})
    prof = cfg.profile("organization")
    _seed([post()])
    wall.decide(wall.build(cfg, prof), "approved", None, 1)
    w = wall.build(cfg, prof)
    assert len(w.shown) == 1
    wall.decide(w, "rejected", [wall.short_id("masto:1")], None)
    assert not wall.build(cfg, prof).shown


def test_deleted_post_disappears_on_next_refresh():
    cfg = load_config(TOML, overlay={})
    prof = cfg.profile("organization")
    _seed([post(id="masto:1"), post(id="masto:2")])
    wall.decide(wall.build(cfg, prof), "approved", None, 2)
    _seed([post(id="masto:2")])  # the next fetch no longer has masto:1
    assert [p.id for p in wall.build(cfg, prof).shown] == ["masto:2"]


def test_real_search_fixture_under_organization_rules(fixture_json):
    posts = parse_bsky_feed(fixture_json("bsky_search.json"))
    src = Source("s", "bluesky_search", {"query": "#photography"})
    prof = _profile("organization", {"allow_accounts": [p.author_handle for p in posts]})  # allow everyone listed
    verdicts = [moderate(p, prof, src, {}) for p in posts]
    dropped = {p.labels[0] if p.labels else "" for p, vd in zip(posts, verdicts) if vd.action == "drop"}
    assert {"porn", "nudity", "!no-unauthenticated", "bot"} <= dropped
    assert all(vd.action == "pending" for p, vd in zip(posts, verdicts) if not p.labels and not p.is_reply)
