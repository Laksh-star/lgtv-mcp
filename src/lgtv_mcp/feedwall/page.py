"""The wall page: 1080p, dark, large type, one featured post at a time with the next ones beside it.

The HTML is a fixed shell; it polls /wall/data every minute and rotates posts itself. Post content
is untrusted, so the script only ever sets textContent (never innerHTML) for it. The QR codes are
SVG made on the Mac by segno.
"""

from __future__ import annotations

from functools import lru_cache
from html import escape

import segno

DATA_POLL_SECONDS = 60


@lru_cache(maxsize=512)
def qr_svg(url: str) -> str:
    return segno.make(url, error="m").svg_inline(dark="#000", light="#fff", border=2, omitsize=True)


CSS = """
:root { --bg:#0b1017; --panel:#141b25; --panel2:#101721; --line:#233041; --fg:#eef2f7; --muted:#9aa8bb;
  --dim:#617086; --accent:#ffb547; --cool:#7cc4ff; --warn:#ff8a7a; }
* { box-sizing:border-box; }
html,body { margin:0; height:100%; background:var(--bg); color:var(--fg); overflow:hidden;
  font-family:"LG Smart UI","Roboto","Helvetica Neue",Arial,sans-serif; }
.frame { height:100vh; padding:4vh 3.6vw 3vh; display:grid; gap:2.2vh; grid-template-rows:auto 1fr auto; }
header { display:flex; justify-content:space-between; align-items:flex-end; gap:3vw; }
.title { font-size:3.4vw; font-weight:700; letter-spacing:-0.02em; line-height:1; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.right { text-align:right; }
.clock { font-size:3.4vw; font-weight:300; line-height:1; color:var(--accent); font-variant-numeric:tabular-nums; }
.meta { font-size:1.2vw; color:var(--dim); margin-top:0.6vh; }
.main { display:grid; grid-template-columns:66fr 34fr; gap:2vw; min-height:0; }
.card { background:var(--panel); border-radius:1.4vw; padding:3vh 2.2vw; min-height:0; display:grid;
  grid-template-rows:auto 1fr auto; gap:2vh; overflow:hidden; }
.who { display:grid; grid-template-columns:auto 1fr auto; gap:1.3vw; align-items:center; min-width:0; }
.av { width:5.6vw; height:5.6vw; border-radius:50%; object-fit:cover; background:var(--line); display:flex;
  align-items:center; justify-content:center; font-size:2.4vw; font-weight:700; color:var(--muted); overflow:hidden; }
.name { font-size:2.3vw; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.sub { font-size:1.45vw; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:0.4vh; }
.chip { font-size:1.15vw; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; padding:0.6vh 0.9vw;
  border-radius:0.6vw; background:var(--panel2); color:var(--cool); white-space:nowrap; }
.chip.bluesky { color:#5fb0ff; } .chip.mastodon { color:#a99bff; } .chip.youtube { color:#ff6b6b; }
.chip.x { color:#e8e8e8; } .chip.rss { color:#ffb547; }
.repost { font-size:1.3vw; color:var(--dim); margin-top:0.3vh; }
.body { display:grid; gap:2vw; min-height:0; }
.body.media { grid-template-columns:1fr 1fr; }
.text { font-size:2.5vw; line-height:1.28; white-space:pre-line; overflow:hidden; display:-webkit-box;
  -webkit-box-orient:vertical; -webkit-line-clamp:11; overflow-wrap:anywhere; }
.body.media .text { font-size:2vw; -webkit-line-clamp:12; }
.body.long .text { font-size:1.9vw; }
.body.media.long .text { font-size:1.6vw; -webkit-line-clamp:15; }
.media-box { min-height:0; display:flex; flex-direction:column; gap:1vh; }
.img-wrap { position:relative; flex:1; min-height:0; border-radius:1vw; overflow:hidden; background:#000; }
.img-wrap img { width:100%; height:100%; object-fit:contain; display:block; }
.badge { position:absolute; left:1vw; top:1vw; font-size:1.3vw; font-weight:700; padding:0.6vh 0.9vw; border-radius:0.6vw;
  background:rgba(0,0,0,0.7); color:#fff; }
.badge.live { background:#d93025; }
.alt { font-size:1.2vw; color:var(--muted); font-style:italic; overflow:hidden; display:-webkit-box;
  -webkit-box-orient:vertical; -webkit-line-clamp:2; }
.hidden-media { font-size:1.5vw; color:var(--muted); }
.foot { display:flex; justify-content:space-between; align-items:flex-end; gap:2vw; }
.scan { display:flex; align-items:center; gap:1vw; font-size:1.2vw; color:var(--dim); text-align:right; }
.qr { width:8.2vw; height:8.2vw; background:#fff; border-radius:0.5vw; padding:0.2vw; }
.qr svg { width:100%; height:100%; display:block; }
.next { display:flex; flex-direction:column; gap:1.6vh; min-height:0; }
.label { font-size:1.2vw; text-transform:uppercase; letter-spacing:0.14em; color:var(--dim); font-weight:600; }
.mini { background:var(--panel2); border-radius:1vw; padding:2vh 1.4vw; display:flex; flex-direction:column; gap:1vh;
  min-height:0; overflow:hidden; flex:1; }
.mini .who { grid-template-columns:auto 1fr; gap:0.9vw; }
.mini .av { width:3.2vw; height:3.2vw; font-size:1.4vw; }
.mini .name { font-size:1.45vw; } .mini .sub { font-size:1.1vw; }
.mini .text { font-size:1.4vw; -webkit-line-clamp:4; }
.empty { grid-column:1 / -1; display:flex; align-items:center; justify-content:center; font-size:2.6vw; color:var(--muted);
  background:var(--panel); border-radius:1.4vw; text-align:center; padding:4vw; }
footer { display:flex; flex-direction:column; gap:1vh; }
.bar { height:0.5vh; background:var(--panel); border-radius:1vh; overflow:hidden; }
.bar i { display:block; height:100%; width:0; background:var(--accent); }
.status { display:flex; justify-content:space-between; gap:2vw; font-size:1.15vw; color:var(--dim); }
.status .warn { color:var(--warn); }
"""

JS = r"""
(function () {
  var PROFILE = %(profile)s, POLL = %(poll)d * 1000;
  var data = null, idx = 0, offset = 0, timer = null, lastOk = Date.now();
  var KEY = "feedwall:" + PROFILE;
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls;
    if (text != null) e.textContent = text; return e; }
  function now() { return Date.now() + offset; }
  var fmts = {};
  function fmt(opts) {  // Intl formatter in the Mac's time zone; the TV's own may be set wrong
    var key = JSON.stringify(opts) + (data && data.tz);
    if (!(key in fmts)) {
      try { fmts[key] = new Intl.DateTimeFormat("en-GB", data && data.tz ? Object.assign({ timeZone: data.tz }, opts) : opts); }
      catch (e) { fmts[key] = null; }
    }
    return fmts[key];
  }
  function ago(iso, dayOnly) {
    if (!iso) return "";
    var t = Date.parse(iso), s = Math.max(0, (now() - t) / 1000);
    if (dayOnly || s > 6 * 86400) { var f = fmt({ day: "numeric", month: "short", year: "numeric" });
      return f ? f.format(new Date(t)) : new Date(t).toDateString(); }
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }
  var PLATFORM = { bluesky: "Bluesky", mastodon: "Mastodon", youtube: "YouTube", x: "X", rss: "RSS" };
  function avatar(p) {
    if (p.avatar) { var i = el("img", "av"); i.src = p.avatar; i.alt = ""; return i; }
    return el("div", "av", (p.name || "?").trim().charAt(0).toUpperCase());
  }
  function who(p, mini) {
    var w = el("div", "who"), info = el("div");
    w.appendChild(avatar(p));
    info.appendChild(el("div", "name", p.name || p.handle));
    var sub = [p.handle, ago(p.created, p.day_only)].filter(Boolean).join(" · ");
    info.appendChild(el("div", "sub", sub));
    if (p.reposted_by && !mini) info.appendChild(el("div", "repost", "↻ reposted by " + p.reposted_by));
    w.appendChild(info);
    if (!mini) w.appendChild(el("div", "chip " + p.platform, PLATFORM[p.platform] || p.platform));
    return w;
  }
  function featured(p) {
    var c = el("section", "card");
    c.appendChild(who(p, false));
    var long = (p.text || "").length > 280;
    var b = el("div", "body" + (p.image ? " media" : "") + (long ? " long" : ""));
    b.appendChild(el("div", "text", p.text));
    if (p.image) {
      var m = el("div", "media-box"), wrap = el("div", "img-wrap"), img = el("img");
      img.src = p.image; img.alt = p.alt || ""; wrap.appendChild(img);
      if (p.live) wrap.appendChild(el("div", "badge live", "● LIVE"));
      else if (p.video) wrap.appendChild(el("div", "badge", "▶ Video — scan to watch"));
      m.appendChild(wrap);
      if (p.alt) m.appendChild(el("div", "alt", p.alt));
      b.appendChild(m);
    }
    c.appendChild(b);
    var f = el("div", "foot"), left = el("div");
    if (p.hidden_media) left.appendChild(el("div", "hidden-media", "📷 This post has a photo — scan to see it"));
    f.appendChild(left);
    if (p.qr) {
      var s = el("div", "scan"), q = el("div", "qr");
      s.appendChild(el("div", null, "Scan to open\non your phone"));
      q.innerHTML = p.qr; /* SVG made on the Mac by segno, not post content */
      s.appendChild(q); f.appendChild(s);
    }
    c.appendChild(f);
    return c;
  }
  function mini(p) {
    var c = el("div", "mini"); c.appendChild(who(p, true)); c.appendChild(el("div", "text", p.text)); return c;
  }
  function render() {
    var main = $("main"); main.textContent = "";
    var posts = (data && data.posts) || [];
    if (!posts.length) {
      main.appendChild(el("div", "empty", data && data.approval ? "Posts will appear here once they’re approved."
        : data ? "Waiting for posts…" : "Loading…"));
      return;
    }
    idx = idx %% posts.length;
    try { localStorage.setItem(KEY, posts[idx].id); } catch (e) {}  // survives a reload (screensaver)
    main.appendChild(featured(posts[idx]));
    var n = el("aside", "next");
    if (posts.length > 1) n.appendChild(el("div", "label", "Up next"));
    for (var k = 1; k <= Math.min(3, posts.length - 1); k++) n.appendChild(mini(posts[(idx + k) %% posts.length]));
    main.appendChild(n);
    var bar = $("bar"); bar.style.transition = "none"; bar.style.width = "0";
    void bar.offsetWidth; bar.style.transition = "width " + data.rotate_seconds + "s linear"; bar.style.width = "100%%";
  }
  function status() {
    if (!data) return;
    $("title").textContent = data.title || "Feed wall";
    var stale = data.sources.filter(function (s) { return !s.ok; });
    var parts = stale.map(function (s) {
      return s.name + ": " + (s.last_updated ? "last updated " + ago(new Date(s.last_updated * 1000).toISOString())
        : "unavailable") + (s.error ? " (" + s.error + ")" : "");
    });
    if (Date.now() - lastOk > POLL * 3) parts.unshift("Can’t reach the Mac; showing the last posts it sent");
    var st = $("st"); st.textContent = parts.join(" · "); st.className = parts.length ? "warn" : "";
    $("meta").textContent = data.posts.length + " post" + (data.posts.length === 1 ? "" : "s") +
      " · " + data.sources.length + " source" + (data.sources.length === 1 ? "" : "s");
    $("attr").textContent = "Public posts, shown with their authors’ names. Scan a code to open the original.";
  }
  function tick() {
    var d = new Date(now()), f = fmt({ hour: "2-digit", minute: "2-digit", hour12: false });
    $("clock").textContent = f ? f.format(d) : ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  function load() {
    var x = new XMLHttpRequest();
    x.open("GET", "/wall/data?profile=" + encodeURIComponent(PROFILE), true);
    x.onload = function () {
      if (x.status !== 200) { status(); return; }
      var first = !data;  // after a reload, carry on after the post shown last instead of starting over
      var cur = first ? stored() : data.posts.length ? data.posts[idx %% data.posts.length].id : null;
      data = JSON.parse(x.responseText); lastOk = Date.now(); offset = data.server_ms - Date.now();
      var keep = -1;
      for (var i = 0; i < data.posts.length; i++) if (data.posts[i].id === cur) keep = i;
      if (first) { idx = keep >= 0 ? keep + 1 : 0; keep = -1; }
      else if (keep >= 0) idx = keep; else if (cur !== null) idx = 0;
      status(); if (keep < 0) render();
    };
    x.onerror = function () { status(); };
    x.send();
  }
  function rotate() { if (data && data.posts.length > 1) { idx++; render(); } status(); }
  tick(); setInterval(tick, 10000);
  load(); setInterval(load, POLL);
  (function loop() { var s = (data && data.rotate_seconds) || 12; timer = setTimeout(function () { rotate(); loop(); }, s * 1000); })();
})();
"""


def render_shell(profile: str) -> str:
    import json
    script = JS % {"profile": json.dumps(profile), "poll": DATA_POLL_SECONDS}
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=1920, height=1080">
<title>Feed wall · {escape(profile)}</title>
<style>{CSS}</style></head>
<body><div class="frame">
<header><div class="title" id="title">Feed wall</div>
<div class="right"><div class="clock" id="clock"></div><div class="meta" id="meta"></div></div></header>
<main class="main" id="main"></main>
<footer><div class="bar"><i id="bar"></i></div>
<div class="status"><span id="st"></span><span id="attr"></span></div></footer>
</div><script>{script}</script></body></html>"""
