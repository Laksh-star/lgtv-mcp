# lgtv-mcp

A local MCP server that lets Claude control an LG webOS TV over the home network.
It's built on [aiowebostv](https://github.com/home-assistant-libs/aiowebostv) (SSAP over secure websocket, port 3001)
and the official MCP Python SDK (`MCPServer`, which is what FastMCP is called in `mcp` 2.x).

Tested on an LG 55UT80406LA (UT80, 2024) running webOS 25 (10.3.2).

## Setup

```bash
uv sync
uv run lgtv-pair            # finds the TV via SSDP. Accept the prompt on the TV within 60 s
uv run lgtv-pair 192.168.1.x --force   # re-pair, or pair a specific IP
```

Pairing writes `~/.config/lgtv/config.json` (mode 0600) with `host`, `mac` and `client_key`.
You can override the stored host and MAC with the `LGTV_HOST` and `LGTV_MAC` environment variables, and the config folder with `LGTV_CONFIG_DIR`.
If the TV gets a new DHCP address, the server finds it again over SSDP, checks it by MAC address, and updates the config.

## Registered in

- **Claude Code** (user scope): `claude mcp add --scope user lgtv -- /opt/homebrew/bin/uv run --directory /path/to/lg-tv lgtv-mcp`
- **Claude desktop**: `mcpServers.lgtv` in `~/Library/Application Support/Claude/claude_desktop_config.json`.
  Quit the app fully (Cmd+Q) first: it keeps this file in memory while it runs and saves over outside edits.
  Then, in Terminal:

  ```bash
  /opt/homebrew/bin/uv run --directory /path/to/lg-tv python scripts/install_claude_desktop.py
  ```

  The script backs up the config to `claude_desktop_config.json.bak-<timestamp>`, refuses to run while the app is open, and leaves other servers untouched.
  It writes absolute paths for `uv` and the project, because the desktop app doesn't use your shell PATH. Reopen the app afterwards.
  If the server misbehaves, its log is `~/Library/Logs/Claude/mcp-server-lgtv.log`.

## Tools

| Tool | What it does |
|---|---|
| `power_on` | Wakes the TV with Wake-on-LAN, plus an SSAP `turnOn` if it's in standby. Waits until it's on, up to 30 s |
| `power_off` | Puts the TV in standby |
| `screen_off` / `screen_on` | Turns the panel off or on while audio keeps playing |
| `get_status` | Power state, current app, input, volume, mute, sound output, media play state, model |
| `set_volume`, `volume_up`, `volume_down`, `mute` | Volume 0–100, step up or down, mute/unmute/toggle |
| `list_inputs` / `switch_input` | Lists HDMI inputs (connected and active flags). Switches by id or label, e.g. `HDMI 3` |
| `list_apps` / `launch_app` | Launches by id or title. Supports `content_id`, `params`, and the shortcuts `youtube_video` (id or URL) and `youtube_search` |
| `media_control` | play, pause, stop, rewind, fast_forward |
| `show_toast` | Shows an on-screen notification |
| `press_button` | Remote buttons: HOME, BACK, OK/ENTER, arrows, MENU, digits, colours… with an optional `repeat` |
| `type_text` | Types into the focused text field via the LG keyboard, optionally pressing Enter. YouTube ignores it, so use `launch_app(youtube_search=…)` there |
| `close_app` | Closes the app on screen, or a named one |
| `channel_up` / `channel_down` | Changes Live TV channel, opening Live TV first if needed. Reports the new channel |
| `show_briefing` | Shows the morning briefing page now, turning the TV on if needed. Takes over the screen unless `take_over` is false |

**Errors.** If the TV is unreachable, in standby, or not paired, the tool returns a one-line message such as "The TV is off (Active Standby). Call power_on first." You never see a stack trace.

**Note on "TV On With Mobile".** With this setting on, the TV usually stays on the network in *Active Standby* after `power_off`, and `power_on` brings it back within about a second. After a while it can drop off the network completely. Then `power_on` wakes it with Wake-on-LAN, which takes about 15 seconds.

**Fixed IP (optional).** In your router's admin page, look for *DHCP reservation* or *Address reservation* and reserve the TV's current IP for its MAC address (in `~/.config/lgtv/config.json`). The server finds the TV again anyway if its address changes, but a reservation avoids the 3-second search.

## Morning briefing

On weekdays at 07:30, the TV shows a full-screen page with:
- the date and a clock
- the weather in Hyderabad: now, high and low, and chance of rain
- today's events from your Google Calendar
- 3 headlines each from The Hindu (India) and BBC News (World)

The page refreshes every 5 minutes.

**How a run decides what to do**

| The TV is… | What happens |
|---|---|
| Off | Turns on, then opens the page in the TV's web browser |
| On, showing Home or the screen saver | Opens the page |
| Playing something, on Live TV or HDMI, or in an app | Leaves the screen alone and shows a one-line notification, e.g. "Morning briefing: 2 meetings today, next at 10:00 · 29°C, rain 0%" |

- **Auto turn-off.** If the page is still on screen after 30 minutes, the TV turns off. If someone switched to something else, it stays on.
- **If the page can't be shown,** the briefing appears as a few notifications instead.
- **A source that fails** (calendar, weather, a feed) is marked "unavailable" on the page, and the rest still shows.

### Pieces

- **Page server.** Runs at `http://<this Mac>:8765/` as the launchd agent `local.lgtv.briefing.server`. Only this Mac and the paired TV may open the page, because it shows your calendar. Other devices get "403 Forbidden" unless their IP is added to `allow_ips`.
- **Scheduled run.** The launchd agent `local.lgtv.briefing` runs `briefing run` at the scheduled times.
- **Calendar address.** It lives only in your login Keychain, never in a file or the repo. Get it from Google Calendar → ⚙️ Settings → your calendar → *Integrate calendar* → **Secret address in iCal format**. Store it with:
  ```bash
  security add-generic-password -U -s lgtv-briefing -a google-calendar-ics -w
  ```
  If you saved it under another name in Keychain Access, set `keychain_service` (and `keychain_account`, or `""` for any account) under `[calendar]` in the settings file. If macOS asks whether `security` may read the item, choose **Always Allow**, or the 07:30 run can't read it.
  Repeating events, moved or skipped occurrences, all-day events and other time zones are all converted to your local time. Cancelled and declined events are left out.
- **Log.** Every run, and every failure with its reason, goes to `~/Library/Logs/lg-tv-briefing.log`. launchd's own output goes to `~/Library/Logs/lg-tv-briefing.launchd.log`.

### Commands (run in this folder)

```bash
uv run briefing now                 # show it now, using the same rules as 07:30 (a notification if the TV is busy)
uv run briefing now --force         # take over the screen even if something is playing
uv run briefing now --auto-off 1    # also test the auto turn-off, after 1 minute
uv run briefing preview             # write the page to ~/Library/Caches/lg-tv-briefing/preview.html
uv run briefing status              # what's installed, Keychain check, wake schedule, recent log lines
```

From Claude, say "show my briefing on the TV", which uses the `show_briefing` tool.

### Change the time or content

Everything lives in `~/.config/lgtv/briefing.toml`:
- **Days, time, auto turn-off:** edit `[schedule]` (`weekdays` uses 1 = Monday … 7 = Sunday, `time = "07:30"`, `auto_off_minutes`, with 0 meaning never). Then run `uv run briefing install` to update launchd. Update the Mac wake time too; `install` prints the matching `pmset` command.
- **City:** edit `[location]` (name, latitude, longitude, timezone).
- **Headlines:** edit the `[[feeds]]` blocks (name, RSS `url`, `count`). Add or remove blocks as you like.

Content changes appear within 5 minutes, with no restart needed.

### Wake the Mac before the briefing

This Mac is set never to sleep, so this is a safety net. It needs your password:

```bash
sudo pmset repeat wakeorpoweron MTWRF 07:25:00
```

`pmset -g sched` shows the schedule. This replaces any other repeating wake schedule; there wasn't one before. If the Mac is fully shut down, it powers on at that time, but the briefing only runs once you're logged in.

### Turn it off

```bash
uv run briefing uninstall           # removes both launchd agents
sudo pmset repeat cancel            # removes the Mac wake schedule
```

Run `uv run briefing install` to turn it back on.

## Development

```bash
uv run python -c "import asyncio; from lgtv_mcp.server import mcp; print([t.name for t in asyncio.run(mcp.list_tools())])"
uv run mcp dev src/lgtv_mcp/server.py   # MCP Inspector
```

Code: `src/lgtv_mcp/tv.py` handles connection, discovery, pairing and WoL. `server.py` defines the tools. `pair.py` is the pairing CLI.
