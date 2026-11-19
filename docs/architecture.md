# Architecture

[← Back to the README](../README.md)

![lgtv-mcp architecture overview](assets/architecture.svg)

The overview above is the whole system on one page. The diagrams below zoom into each part:

- [Control path](#control-path): how a tool call reaches the TV
- [Movie night](#movie-night): from "what should we watch" to the film playing
- [Morning briefing](#morning-briefing): the scheduled jobs, the page server and the data sources
- [One scheduled run](#one-scheduled-run): what happens at 07:30
- [Briefing controls](#briefing-controls): pausing and resuming from chat
- [Feed wall](#feed-wall): sources, moderation, the wall page and its schedule
- [One wall refresh](#one-wall-refresh): from the sources to the posts on screen
- [Showing and ending a wall](#showing-and-ending-a-wall): busy check, takeover, end time, screensaver
- [Moderation order](#moderation-order): which rule drops, holds or shows a post
- [The TV's screensaver](#the-tvs-screensaver): why the page server keeps clearing it

Everything runs on the Mac and the home network. The only traffic that leaves the house is outbound: weather, RSS, the calendar feed, Wikidata lookups, the feed wall's sources and the images on the wall, and, through Screenward, TMDB.

## Control path

Claude starts `lgtv-mcp` as a stdio MCP server. The server keeps one SSAP websocket session to the TV, rediscovers it over SSDP if its IP changes (matched by MAC address), and wakes it with Wake-on-LAN.

```mermaid
flowchart LR
    subgraph Mac["🖥️ Mac mini"]
        CC["Claude Code"]
        CD["Claude desktop"]
        S["lgtv-mcp<br/>server.py · 38 tools"]
        T["TV manager<br/>tv.py"]
        CFG[("~/.config/lgtv/<br/>config.json")]
        CC -- "stdio (MCP)" --> S
        CD -- "stdio (MCP)" --> S
        S --> T
        T <--> CFG
    end
    subgraph Home["🏠 Home Wi-Fi"]
        TV["📺 LG webOS TV"]
    end
    T -- "SSAP over wss :3001<br/>commands + live state" --> TV
    T -. "Wake-on-LAN (UDP :9)" .-> TV
    T -. "SSDP discovery (UDP :1900)" .-> TV
```

- **Pairing key:** `~/.config/lgtv/config.json` (mode 0600) holds the TV's IP, MAC and client key.
- **A TV that moved:** the router can give the TV a new address while it's off. `TV.reachable()` tries the saved address, then looks for the TV over SSDP and saves the new one. While waking the TV, `ensure_on()` searches like this every 8 s, so a TV that comes up at a new address is still found.
- **Shutting down:** for about 2 minutes after a power-off the TV keeps its ports open but closes every connection (WebSocket code 1008). That counts as "unavailable", not a crash.
- **Errors:** every TV or network failure becomes a short tool error ("the TV is off", "the TV didn't answer in time"), never a stack trace. Tools that need a lit screen refuse while the TV is in standby.

## Movie night

`/movienight` is a skill that uses two MCP servers: Screenward picks the film, and `lgtv` plays it.

```mermaid
sequenceDiagram
    autonumber
    actor U as You
    participant C as Claude (/movienight)
    participant SW as Screenward MCP
    participant L as lgtv-mcp
    participant WD as Wikidata
    participant TV as LG TV
    U->>C: "a feel-good Malayalam film for the family"
    C->>L: get_status, list_apps
    L-->>C: TV on · Netflix, Prime Video, JioHotstar…
    C->>SW: shortlist streaming in India on those apps<br/>(minus watch_history.json)
    SW-->>C: candidates
    C-->>U: 3 picks, one line each
    U->>C: pick one
    C->>L: play_title(tmdb_id)
    L->>WD: TMDB id → Netflix / Prime id (cached in title_ids.json)
    WD-->>L: app content id
    L->>TV: deep link into the installed app
    alt Prime Video
        L->>TV: OK on "Who's watching?", then Play on the title page
    end
    L->>TV: check a new media session is playing
    L-->>C: playing ✓ (logged to watch_history.json)
```

If Wikidata has no id, `play_title` answers `needs_lookup`, Claude web-searches the id and calls again with `app_content_id`. Apps with no working deep link (JioHotstar, ZEE5) open with a toast instead.

## Morning briefing

Two launchd agents run the briefing: a calendar job at 07:30 on weekdays, and a page server that is always up. Both gather the same data; each source fails on its own, and the page shows "unavailable" for just that part. The page server also serves the [feed wall](#feed-wall) and [keeps the TV's screensaver off](#the-tvs-screensaver) both pages.

```mermaid
flowchart TB
    subgraph Mac["🖥️ Mac mini"]
        L1["⏰ launchd<br/>local.lgtv.briefing<br/>Mon–Fri 07:30"]
        L2["launchd<br/>local.lgtv.briefing.server"]
        R["briefing run<br/>show.py"]
        W["page server :8765<br/>web.py · page.py"]
        G["gather()<br/>sources.py"]
        P[("briefing-state.json<br/>pause")]
        K[("🔐 Keychain<br/>iCal secret address")]
        LOG[["~/Library/Logs/<br/>lg-tv-briefing.log"]]
        L1 --> R
        R -. "paused today? exit" .-> P
        L2 --> W
        R --> G
        W --> G
        G --> K
        R --> LOG
        W --> LOG
    end
    subgraph Net["☁️ Internet"]
        GC["Google Calendar"]
        OM["Open-Meteo"]
        RSS["The Hindu · BBC RSS"]
    end
    G --> GC & OM & RSS
    R -- "power on · busy check ·<br/>open browser · toast · auto-off" --> TV["📺 TV"]
    TV -- "GET / every 5 min<br/>(only the TV and this Mac allowed)" --> W
```

The Mac wakes at 07:25 (`pmset repeat wakeorpoweron`), five minutes before the job.

## One scheduled run

```mermaid
sequenceDiagram
    autonumber
    participant L as launchd (07:30)
    participant R as briefing run
    participant TV as LG TV
    participant W as page server
    L->>R: start
    alt not a briefing day, or paused
        R->>R: log "skipping" and exit
    end
    R->>R: gather calendar, weather, headlines
    alt TV off
        R->>TV: Wake-on-LAN / turnOn, repeated for up to 90 s
        Note over R,TV: still off? try the whole run again in 2 min
    else TV busy (playing, Live TV, an app)
        R->>TV: toast "Morning briefing: …"
        Note over R,TV: done, screen left alone
    end
    R->>TV: launch browser → http://mac:8765/
    TV->>W: GET /
    R->>W: poll /api/views
    W-->>R: TV loaded the page ✓
    alt page never loads
        R->>TV: fallback toasts
    end
    loop every 5 s, until the auto-off time
        W->>TV: screensaver up over the page? → dismiss it, reload the page
    end
    R->>R: wait 30 min
    R->>TV: still showing the page? → power off
```

## Briefing controls

`pause_briefing`, `resume_briefing`, `briefing_status` and `restart_briefing_server` (and the matching `briefing` commands) all go through `control.py`. A pause is a single date, the last day to skip, so it ends by itself.

```mermaid
flowchart LR
    C["Claude chat<br/>“no briefing tomorrow”"] -- pause_briefing --> CT["control.py"]
    CLI["uv run briefing pause"] --> CT
    CT -- "paused_through: 2026-11-18" --> P[("~/.config/lgtv/<br/>briefing-state.json")]
    J["07:30 job<br/>briefing run"] -- "reads" --> P
    J -- "date ≤ paused_through" --> X["skip, log it"]
    J -- "otherwise" --> S["show the briefing"]
    C2["“turn it back on”"] -- resume_briefing --> CT
    CT -- "deletes" --> P
    C3["“restart the page”"] -- restart_briefing_server --> K["launchctl kickstart -k<br/>local.lgtv.briefing.server"]
```

Changing the time or the days isn't a chat command: edit `~/.config/lgtv/briefing.toml`, run `uv run briefing install`, and set a matching `sudo pmset` wake time.

## Feed wall

The feed wall reuses the briefing's pieces: the page server, the TV-only guard, the busy check, launchd and the Keychain. Two profiles decide how it behaves: **personal** never takes over a busy TV (a toast instead), and **organization** takes over a dedicated screen during its show hours with strict moderation.

```mermaid
flowchart TB
    subgraph Mac["🖥️ Mac mini"]
        T["MCP tools<br/>show_feed_wall · approve_posts · …"]
        CLI["uv run feedwall …"]
        LD["⏰ launchd<br/>local.lgtv.feedwall.&lt;profile&gt;<br/>start of show hours"]
        SH["show.py<br/>busy check · take over · stop"]
        CT["control.py<br/>status · approvals · pauses · sources"]
        W["page server :8765<br/>/wall · /wall/data · /wall/img<br/>watcher: end time, screensaver"]
        WL["wall.py<br/>refresh · build"]
        SRC["sources.py<br/>RSS · YouTube · Bluesky · Mastodon · X"]
        MOD["moderation.py"]
        CFG[("feedwall.toml<br/>+ feedwall-sources.json")]
        ST[("feedwall-state.json · decisions<br/>(file locks)")]
        CA[("~/Library/Caches/lg-tv-feedwall<br/>a JSON per source · images")]
        K[("🔐 Keychain<br/>Bluesky app password · YouTube key")]
        T --> SH & CT
        CLI --> SH & CT
        LD --> SH
        SH --> WL
        CT --> WL
        W --> WL
        WL --> SRC & MOD
        WL <--> CA
        SRC --> K
        CT <--> ST
        SH <--> ST
        WL --> CFG
    end
    subgraph Net["☁️ Internet"]
        N["RSS / Atom · YouTube feeds + Data API<br/>Bluesky AppView / your PDS · Mastodon<br/>publish.x.com oEmbed · image hosts"]
    end
    SRC --> N
    W -- "fetches registered images only" --> N
    SH -- "power on · toast · open browser" --> TV["📺 TV"]
    TV -- "GET /wall, then /wall/data every 60 s<br/>(only the TV and this Mac allowed)" --> W
```

- **Sources fail on their own.** Each source's last good posts are kept in its own JSON file. A source that fails keeps showing them, and the wall's footer says when it last updated.
- **Images go through the Mac.** The TV only ever talks to the Mac. `/wall/img/<key>` serves images the wall registered (no open proxy), capped at 8 MB and cached for 7 days.
- **The page is locked down.** Post text is set with `textContent` only, and the page sends a strict Content-Security-Policy.
- **Credentials** come only from the Keychain. A Bluesky login is saved there too and refreshed, because `createSession` is limited to 10 a day.

## One wall refresh

The page asks for `/wall/data` every 60 s. Each request refreshes the sources that are due and rebuilds the wall from their latest fetch, so a deleted post disappears and an edited one goes back for approval within one refresh.

```mermaid
flowchart LR
    REQ["TV: GET /wall/data"] --> RF["refresh()<br/>per profile, one at a time"]
    RF --> DUE{"source tried in the last<br/>3 min? (YouTube: 15)"}
    DUE -- yes --> KEEP["use its cache"]
    DUE -- no --> F["fetch<br/>(ETag / Last-Modified)"]
    F -- ok --> SAVE["save posts, ok_at"]
    F -- error --> ERR["keep last good posts,<br/>record the error"]
    KEEP & SAVE & ERR --> B["build()"]
    B --> D["drop duplicates"]
    D --> M["moderate each post<br/>show · pending · drop"]
    M --> I["interleave: one post from<br/>each source in turn, up to 30"]
    I --> PD["page_data(): register images,<br/>QR codes, stale-source notes"]
    PD --> PAGE["JSON to the page"]
```

The page rotates a post every 12 s and remembers the post it was on, so a reload carries on from there instead of starting over.

## Showing and ending a wall

```mermaid
sequenceDiagram
    autonumber
    participant C as show_feed_wall / launchd
    participant S as show.py
    participant TV as LG TV
    participant W as page server
    C->>S: profile, minutes (or the show-hours window)
    alt launchd run: paused, or outside the hours
        S->>S: log "skipping" and stop
    end
    S->>S: refresh + build · it ends at the earlier of minutes and the window
    alt TV off
        S->>TV: Wake-on-LAN until on (finds a moved TV)
    else personal profile and the TV is busy
        S->>TV: toast "Feed wall: 30 posts ready. Latest, …"
        Note over S,TV: done, screen left alone
    end
    S->>TV: launch browser → http://mac:8765/wall?profile=…
    TV->>W: GET /wall, /wall/data
    S->>W: poll /api/wall/views until the TV loaded it
    S->>S: save "showing" (profile, end, at_end)
    loop every 5 s while showing
        W->>TV: screensaver up over the wall? → dismiss it, reload the wall
    end
    W->>TV: end time reached → home screen (or power off, at_end = "off")
```

`stop_feed_wall` ends it the same way at once. If someone has switched to something else by then, the TV is left alone.

## Moderation order

`moderation.py` decides each post in this order; the first rule that matches wins. The always-on rules apply to both profiles; the rest are profile settings, strict by default for organization.

```mermaid
flowchart TB
    P["post"] --> V{"public?"}
    V -- no --> X["drop"]
    V -- yes --> L{"adult / violent / hide label,<br/>or !no-unauthenticated?"}
    L -- yes --> X
    L -- no --> BA{"blocked account, or a repost<br/>or quote of one?"}
    BA -- yes --> X
    BA -- no --> BW{"blocklist word or pattern in text,<br/>name, alt text, link title, CW?"}
    BW -- yes --> X
    BW -- no --> PR{"reply · repost · bot ·<br/>sensitive, if the profile drops them?"}
    PR -- yes --> X
    PR -- no --> OD{"open source (hashtag, search, feed)<br/>and the account opted out of discovery?"}
    OD -- yes --> X
    OD -- no --> AL{"open source and not on the allowlist?<br/>(X links you added are exempt)"}
    AL -- yes --> X
    AL -- no --> AQ{"approval queue on?"}
    AQ -- no --> SHOW["show"]
    AQ -- yes --> DEC{"decision"}
    DEC -- none --> PEND["pending"]
    DEC -- rejected --> X
    DEC -- "approved, but edited since" --> PEND
    DEC -- approved --> SHOW
```

Approvals live in `~/.config/lgtv/feedwall-decisions.json` with the post's revision, so an edit sends it back to the queue.

## The TV's screensaver

webOS puts up its screensaver about 3 minutes after the last remote input, even over a page that keeps changing. There's no way to prevent it: `turnOnScreen` is refused while the screen is active and only dismisses the screensaver once it's up. After that the browser stays black until the page is opened again.

So the page server watches while one of its pages is on screen, using `screensaver.py`:

| Page | Watched while | Then |
|---|---|---|
| Feed wall | a wall is "showing" and the TV loaded it in the last 150 s | dismiss, reload the wall (it carries on from the post it was on) |
| Briefing | the TV keeps reloading it (every 5 min), more recently than the wall, and for up to its auto-off time (30 min when auto-off is 0) | dismiss, reload the briefing |

It checks every 5 s, so the screensaver shows for a few seconds at most. If the TV is off or unreachable, it waits a minute before trying again. After a page-server restart the briefing is picked up at the TV's next 5-minute reload.
