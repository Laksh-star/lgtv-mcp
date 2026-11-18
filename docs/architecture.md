# Architecture

[← Back to the README](../README.md)

![lgtv-mcp architecture overview](assets/architecture.svg)

The overview above is the whole system on one page. The diagrams below zoom into each part:

- [Control path](#control-path): how a tool call reaches the TV
- [Movie night](#movie-night): from "what should we watch" to the film playing
- [Morning briefing](#morning-briefing): the scheduled jobs, the page server and the data sources
- [One scheduled run](#one-scheduled-run): what happens at 07:30
- [Briefing controls](#briefing-controls): pausing and resuming from chat

Everything runs on the Mac and the home network. The only traffic that leaves the house is outbound: weather, RSS, the calendar feed, Wikidata lookups and, through Screenward, TMDB.

## Control path

Claude starts `lgtv-mcp` as a stdio MCP server. The server keeps one SSAP websocket session to the TV, rediscovers it over SSDP if its IP changes (matched by MAC address), and wakes it with Wake-on-LAN.

```mermaid
flowchart LR
    subgraph Mac["🖥️ Mac mini"]
        CC["Claude Code"]
        CD["Claude desktop"]
        S["lgtv-mcp<br/>server.py · 30 tools"]
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

Two launchd agents run the briefing: a calendar job at 07:30 on weekdays, and a page server that is always up. Both gather the same data; each source fails on its own, and the page shows "unavailable" for just that part.

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
