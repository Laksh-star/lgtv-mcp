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
- **Claude desktop**: `mcpServers.lgtv` in `~/Library/Application Support/Claude/claude_desktop_config.json`
  (a backup was saved alongside it as `claude_desktop_config.json.bak-<timestamp>`). Restart the app to load it.

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

**Errors.** If the TV is unreachable, in standby, or not paired, the tool returns a one-line message such as "The TV is off (Active Standby). Call power_on first." You never see a stack trace.

**Note on "TV On With Mobile".** With this setting on, the TV stays on the network in *Active Standby* after `power_off`. That is why `power_on` works within about a second.

**Fixed IP (optional).** In your router's admin page, look for *DHCP reservation* or *Address reservation* and reserve the TV's current IP for its MAC address (in `~/.config/lgtv/config.json`). The server finds the TV again anyway if its address changes, but a reservation avoids the 3-second search.

## Development

```bash
uv run python -c "import asyncio; from lgtv_mcp.server import mcp; print([t.name for t in asyncio.run(mcp.list_tools())])"
uv run mcp dev src/lgtv_mcp/server.py   # MCP Inspector
```

Code: `src/lgtv_mcp/tv.py` handles connection, discovery, pairing and WoL. `server.py` defines the tools. `pair.py` is the pairing CLI.
