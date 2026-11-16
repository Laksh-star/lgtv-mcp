"""MCP server exposing LG webOS TV controls as tools."""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, quote_plus, urlparse

import aiohttp
from aiowebostv import endpoints as ep
from aiowebostv.buttons import BUTTONS
from aiowebostv.exceptions import (
    WebOsTvCommandError,
    WebOsTvCommandTimeoutError,
    WebOsTvServiceNotFoundError,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .tv import TV, TVError, port_open

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

mcp = MCPServer(
    name="lgtv",
    instructions=(
        "Controls the user's LG webOS TV on the home network. Call get_status first if unsure "
        "whether the TV is on. If a tool says the TV is off or unreachable, call power_on."
    ),
)
tv = TV()

YOUTUBE_APP = "youtube.leanback.v4"

BUTTON_ALIASES = {
    "OK": "ENTER",
    "SELECT": "ENTER",
    "CENTER": "ENTER",
    "VOL_UP": "VOLUMEUP",
    "VOL_DOWN": "VOLUMEDOWN",
    "CH_UP": "CHANNELUP",
    "CH_DOWN": "CHANNELDOWN",
    "FF": "FASTFORWARD",
    "FAST_FORWARD": "FASTFORWARD",
    "SETTINGS": "MENU",
}


def tv_tool(fn):
    """Turn every TV/network failure into a short, readable tool error (no stack traces)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except TVError as e:
            raise ToolError(str(e)) from e
        except WebOsTvServiceNotFoundError as e:
            raise ToolError(f"This TV doesn't support that command ({e}).") from e
        except WebOsTvCommandTimeoutError as e:
            raise ToolError("The TV didn't answer in time. It may be busy or turning off; try again.") from e
        except WebOsTvCommandError as e:
            if tv.client is None or not tv.client.is_connected():
                raise ToolError(tv.unreachable_message()) from e
            raise ToolError(f"The TV rejected the command: {e}") from e
        except (TimeoutError, OSError, aiohttp.ClientError, ConnectionError) as e:
            raise ToolError(tv.unreachable_message()) from e

    return wrapper


SYSTEM_APP_TITLES = {"com.webos.app.home": "Home", "com.webos.app.livetv": "Live TV"}


async def _tv_on():
    """Connect and make sure the TV is actually on, not just reachable in standby."""
    client = await tv.connect()
    if not client.tv_state.is_on:
        state = (client.tv_state.power_state or {}).get("state", "standby")
        raise ToolError(f"The TV is off ({state}). Call power_on first.")
    return client


def _app_titles(client) -> dict[str, str]:
    titles = dict(SYSTEM_APP_TITLES)
    titles.update({app_id: app.get("title", app_id) for app_id, app in client.tv_state.apps.items()})
    return titles


def _status(client) -> dict[str, Any]:
    s = client.tv_state
    titles = _app_titles(client)
    app_id = s.current_app_id
    current_input = s.inputs.get(app_id) if app_id else None
    system = client.tv_info.system or {}
    return {
        "power": (s.power_state or {}).get("state", "On" if s.is_on else "Unknown"),
        "is_on": s.is_on,
        "screen_on": s.is_screen_on,
        "current_app": {"id": app_id, "title": titles.get(app_id, app_id)} if app_id else None,
        "input": (
            {"id": current_input.get("id"), "label": current_input.get("label")}
            if current_input
            else ("Live TV" if app_id == "com.webos.app.livetv" else "app (no external input)")
        ),
        "volume": s.volume,
        "muted": s.muted,
        "sound_output": s.sound_output,
        "media": s.media_state or None,
        "tv": {
            "host": tv.host,
            "model": system.get("modelName"),
            "webos_release": (client.tv_info.hello or {}).get("deviceOSReleaseVersion"),
        },
    }


def _resolve_app(client, app: str) -> str:
    apps = _app_titles(client)
    if app in apps:
        return app
    needle = app.strip().lower()
    exact = [i for i, t in apps.items() if t.lower() == needle]
    if exact:
        return exact[0]
    partial = [i for i, t in apps.items() if needle in t.lower() or needle in i.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(f"{apps[i]} ({i})" for i in partial)
        raise ToolError(f"'{app}' matches several apps: {names}. Use the exact id.")
    # Not a launch point (e.g. a hidden system app); let the TV decide.
    return app


def _youtube_id(video: str) -> str:
    video = video.strip()
    if "://" not in video and "/" not in video:
        return video
    u = urlparse(video)
    if u.hostname and u.hostname.endswith("youtu.be"):
        return u.path.lstrip("/")
    if "v" in parse_qs(u.query):
        return parse_qs(u.query)["v"][0]
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
        return parts[1]
    raise ToolError(f"Couldn't find a YouTube video id in {video!r}.")


# ---------------------------------------------------------------- power


@mcp.tool()
@tv_tool
async def power_on() -> str:
    """Turn the TV on (Wake-on-LAN magic packet, plus a webOS wake request if it is in standby)."""
    return await tv.ensure_on()


@mcp.tool()
@tv_tool
async def power_off() -> str:
    """Turn the TV off (to standby)."""
    if tv.host and not await port_open(tv.host):
        return "The TV is already off (not reachable on the network)."
    client = await tv.connect()
    if not client.tv_state.is_on:
        return f"The TV is already off ({client.tv_state.power_state.get('state', 'standby')})."
    await client.power_off()
    return "The TV is turning off."


@mcp.tool()
@tv_tool
async def screen_off() -> str:
    """Turn only the screen off; sound and apps keep running (good for music)."""
    client = await _tv_on()
    await client.set_screen_state(False)
    return "Screen turned off (audio keeps playing). Use screen_on or any remote button to bring it back."


@mcp.tool()
@tv_tool
async def screen_on() -> str:
    """Turn the screen back on after screen_off."""
    client = await _tv_on()
    await client.set_screen_state(True)
    return "Screen turned on."


@mcp.tool()
@tv_tool
async def get_status() -> dict[str, Any]:
    """Power state, current app, input, volume/mute and sound output of the TV."""
    if tv.host and not await port_open(tv.host):
        return {"power": "Off (unreachable)", "is_on": False, "tv": {"host": tv.host},
                "hint": "Call power_on to wake it."}
    client = await tv.connect()
    try:
        client.tv_state.power_state = await client.get_power_state()
    except WebOsTvCommandError:
        pass
    return _status(client)


# ---------------------------------------------------------------- volume


@mcp.tool()
@tv_tool
async def set_volume(level: Annotated[int, Field(ge=0, le=100, description="Volume 0-100")]) -> str:
    """Set the TV volume to an exact level."""
    client = await _tv_on()
    await client.set_volume(level)
    return f"Volume set to {level}."


async def _step_volume(up: bool, steps: int) -> str:
    client = await _tv_on()
    for _ in range(steps):
        await (client.volume_up() if up else client.volume_down())
    await asyncio.sleep(0.3)
    vol = await client.get_volume()
    return f"Volume {'up' if up else 'down'} {steps} step(s), now {vol}."


@mcp.tool()
@tv_tool
async def volume_up(steps: Annotated[int, Field(ge=1, le=20)] = 1) -> str:
    """Raise the volume by a number of steps."""
    return await _step_volume(True, steps)


@mcp.tool()
@tv_tool
async def volume_down(steps: Annotated[int, Field(ge=1, le=20)] = 1) -> str:
    """Lower the volume by a number of steps."""
    return await _step_volume(False, steps)


@mcp.tool()
@tv_tool
async def mute(muted: Annotated[bool | None, Field(description="true=mute, false=unmute, omit=toggle")] = None) -> str:
    """Mute, unmute, or toggle mute."""
    client = await _tv_on()
    target = (not client.tv_state.muted) if muted is None else muted
    await client.set_mute(target)
    return "Muted." if target else "Unmuted."


# ---------------------------------------------------------------- inputs & apps


@mcp.tool()
@tv_tool
async def list_inputs() -> list[dict[str, Any]]:
    """List the TV's external inputs (HDMI etc.) and which one is active."""
    client = await _tv_on()
    current = client.tv_state.current_app_id
    return [
        {
            "id": i.get("id"),
            "label": i.get("label"),
            "app_id": app_id,
            "connected": i.get("connected"),
            "active": app_id == current,
        }
        for app_id, i in client.tv_state.inputs.items()
    ]


@mcp.tool()
@tv_tool
async def switch_input(input: Annotated[str, Field(description="Input id (e.g. HDMI_1) or label (e.g. 'HDMI 1')")]) -> str:
    """Switch to an external input."""
    client = await _tv_on()
    needle = input.strip().lower().replace(" ", "").replace("_", "")
    for app_id, i in client.tv_state.inputs.items():
        keys = {str(i.get("id", "")), str(i.get("label", "")), app_id}
        if any(k.lower().replace(" ", "").replace("_", "") == needle for k in keys):
            await client.set_input(i["id"])
            return f"Switched to {i.get('label') or i['id']}."
    names = ", ".join(f"{i.get('label')} ({i.get('id')})" for i in client.tv_state.inputs.values())
    raise ToolError(f"No input matches {input!r}. Available: {names}")


@mcp.tool()
@tv_tool
async def list_apps(
    include_hidden: Annotated[bool, Field(description="Also list hidden/system apps")] = False,
) -> list[dict[str, Any]]:
    """List apps installed on the TV (id and title)."""
    client = await _tv_on()
    current = client.tv_state.current_app_id
    if include_hidden:
        apps = await client.get_apps_all() or []
        return [
            {"id": a.get("id"), "title": a.get("title"), "system": a.get("systemApp"),
             "removable": a.get("removable"), "visible": a.get("visible")}
            for a in apps
        ]
    return sorted(
        (
            {"id": app_id, "title": a.get("title"), "removable": a.get("removable"),
             "running": app_id == current}
            for app_id, a in client.tv_state.apps.items()
        ),
        key=lambda a: (a["title"] or "").lower(),
    )


@mcp.tool()
@tv_tool
async def launch_app(
    app: Annotated[str, Field(description="App id (e.g. youtube.leanback.v4, netflix) or title (e.g. 'YouTube')")],
    content_id: Annotated[str | None, Field(description="Optional contentId deep link passed to the app")] = None,
    params: Annotated[dict[str, Any] | None, Field(description="Optional launch params, e.g. {\"contentTarget\": \"https://www.youtube.com/tv?v=VIDEO_ID\"}")] = None,
    youtube_video: Annotated[str | None, Field(description="Shortcut: YouTube video id or URL to play (opens the YouTube app)")] = None,
    youtube_search: Annotated[str | None, Field(description="Shortcut: search YouTube for this text (opens the YouTube app)")] = None,
) -> str:
    """Open an app, optionally with content: a YouTube video or search, or any app's contentId/params."""
    client = await _tv_on()
    params = dict(params or {})
    if youtube_video or youtube_search:
        app_id = YOUTUBE_APP
        if youtube_video:
            params["contentTarget"] = f"https://www.youtube.com/tv?v={_youtube_id(youtube_video)}"
        else:
            params["contentTarget"] = f"https://www.youtube.com/tv?launch=search&vq={quote_plus(youtube_search)}"
    else:
        app_id = _resolve_app(client, app)
    payload: dict[str, Any] = {"id": app_id}
    if content_id:
        payload["contentId"] = content_id
    if params:
        payload["params"] = params
    known = app_id in _app_titles(client)
    try:
        await client.request(ep.LAUNCH, payload)
    except WebOsTvCommandTimeoutError:
        raise
    except WebOsTvCommandError as e:
        if not known and client.is_connected():
            raise ToolError(f"No installed app matches {app!r}. Use list_apps to see what is installed.") from e
        raise
    title = _app_titles(client).get(app_id, app_id)
    what = f" ({youtube_search!r} search)" if youtube_search else (" (video)" if youtube_video else "")
    return f"Launched {title}{what}."


# ---------------------------------------------------------------- media, toast, remote


@mcp.tool()
@tv_tool
async def media_control(action: Literal["play", "pause", "stop", "rewind", "fast_forward"]) -> str:
    """Control playback in the current app: play, pause, stop, rewind, fast_forward."""
    client = await _tv_on()
    await getattr(client, action)()
    return f"Sent {action.replace('_', ' ')}."


@mcp.tool()
@tv_tool
async def show_toast(message: Annotated[str, Field(min_length=1, max_length=200)]) -> str:
    """Show a short notification bubble on the TV screen."""
    client = await _tv_on()
    await client.send_message(message)
    return "Notification shown on the TV."


@mcp.tool()
@tv_tool
async def press_button(
    button: Annotated[str, Field(description="HOME, BACK, OK/ENTER, UP, DOWN, LEFT, RIGHT, EXIT, MENU, INFO, PLAY, PAUSE, digits 0-9, RED/GREEN/YELLOW/BLUE, ...")],
    repeat: Annotated[int, Field(ge=1, le=20)] = 1,
) -> str:
    """Press a remote-control button (optionally several times)."""
    name = button.strip().upper().replace(" ", "_")
    name = BUTTON_ALIASES.get(name, name)
    if name not in BUTTONS:
        raise ToolError(f"Unknown button {button!r}. Known: {', '.join(BUTTONS)}")
    client = await _tv_on()
    for n in range(repeat):
        await client.button(name)
        if n + 1 < repeat:
            await asyncio.sleep(0.25)
    return f"Pressed {name}" + (f" x{repeat}." if repeat > 1 else ".")


@mcp.tool()
@tv_tool
async def type_text(
    text: Annotated[str, Field(min_length=1, max_length=500, description="Text to type into the focused text field")],
    submit: Annotated[bool, Field(description="Press Enter after typing (e.g. to run a search)")] = False,
    replace: Annotated[bool, Field(description="Replace what's already in the field instead of appending")] = False,
) -> str:
    """Type text into the text field that has focus on the TV (search boxes, browser, sign-in forms).

    A text field must be focused first, with the LG on-screen keyboard showing. Apps with their own
    keyboard ignore this, notably YouTube: to search YouTube use launch_app(youtube_search=...) instead.
    """
    client = await _tv_on()
    try:
        await client.request(ep.INSERT_TEXT, {"text": text, "replace": 1 if replace else 0})
        if submit:
            await client.request(ep.SEND_ENTER)
    except WebOsTvCommandError as e:
        if client.is_connected():
            raise ToolError(
                "The TV didn't accept the text. Make sure a text field is selected and the "
                "on-screen keyboard is showing, then try again."
            ) from e
        raise
    return f"Typed {text!r}" + (" and pressed Enter." if submit else ".")


@mcp.tool()
@tv_tool
async def close_app(
    app: Annotated[str | None, Field(description="App id or title to close; omit to close the app in the foreground")] = None,
) -> str:
    """Close an app (by default the one currently on screen)."""
    client = await _tv_on()
    app_id = _resolve_app(client, app) if app else client.tv_state.current_app_id
    if not app_id or app_id == "com.webos.app.home":
        return "No app is open (the home screen is showing)."
    title = _app_titles(client).get(app_id, app_id)
    try:
        await client.close_app(app_id)
    except WebOsTvCommandError as e:
        if client.is_connected():
            raise ToolError(f"{title} isn't running, so there was nothing to close.") from e
        raise
    return f"Closed {title}."


async def _step_channel(up: bool) -> str:
    client = await _tv_on()
    if client.tv_state.current_app_id != "com.webos.app.livetv":
        await client.launch_app("com.webos.app.livetv")
        await asyncio.sleep(2.5)
    try:
        await (client.channel_up() if up else client.channel_down())
    except WebOsTvCommandError as e:
        if client.is_connected():
            raise ToolError(
                "The TV couldn't change channel. Live TV may have no channels tuned "
                "(no antenna or cable box set up)."
            ) from e
        raise
    await asyncio.sleep(1)
    try:
        ch = await client.get_current_channel()
        name = ch.get("channelName") or ch.get("channelNumber")
    except WebOsTvCommandError:
        name = None
    return f"Channel {'up' if up else 'down'}" + (f", now on {name}." if name else ".")


@mcp.tool()
@tv_tool
async def channel_up() -> str:
    """Next Live TV channel (opens Live TV first if another app is showing)."""
    return await _step_channel(True)


@mcp.tool()
@tv_tool
async def channel_down() -> str:
    """Previous Live TV channel (opens Live TV first if another app is showing)."""
    return await _step_channel(False)


@mcp.tool()
@tv_tool
async def show_briefing(
    take_over: Annotated[bool, Field(description="Take over the screen even if something is playing. "
                                                 "false = only show a one-line notification when the TV is busy")] = True,
) -> str:
    """Show the morning briefing on the TV: date, weather, today's calendar and top headlines.

    Turns the TV on if needed and opens the briefing page in the TV's web browser
    (falls back to on-screen notifications if the page can't be shown).
    """
    from .briefing.config import load_config, setup_logging
    from .briefing.show import show

    setup_logging().info("run: requested through the show_briefing tool")
    return await show(tv, load_config(), take_over=take_over)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
