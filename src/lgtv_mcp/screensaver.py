"""Keep the TV's screensaver off the briefing page and the feed wall.

webOS puts up its screensaver a few minutes after the last remote input (about 3 on a 55UT80, webOS 10),
even over a web page that keeps changing, and offers no way to prevent it: turnOnScreen is refused while
the screen is active ("sub state must be 'screen off'") and only dismisses the screensaver once it's up.
After that the browser comes back black, so the page is opened again too. The page server checks every
few seconds while one of its pages is on screen.
"""

from __future__ import annotations

from aiowebostv import endpoints as ep

BROWSER = "com.webos.app.browser"
SCREEN_SAVER = "Screen Saver"
CHECK_SECONDS = 5  # how long the screensaver can stay up over a page


async def over_browser(client) -> bool:
    """Is the screensaver up with the web browser underneath?"""
    state = (await client.get_power_state()).get("state")
    return state == SCREEN_SAVER and client.tv_state.current_app_id == BROWSER


async def dismiss_and_reload(client, url: str) -> None:
    await client.request("com.webos.service.tvpower/power/turnOnScreen", {"standbyMode": "active"})
    await client.request(ep.LAUNCH, {"id": BROWSER, "params": {"target": url}})


def briefing_on_screen(now: float, viewed_at: float, since: float, wall_viewed_at: float,
                       refresh_seconds: int, keep_minutes: int) -> bool:
    """Should the briefing still be kept on screen?

    viewed_at: the TV's last load of the briefing page, which reloads itself every refresh_seconds.
    since: when that run of loads began. wall_viewed_at: the TV's last request from the feed wall.
    The briefing is kept up only while it is the page the TV last loaded, still reloading, and for
    keep_minutes after it first appeared (its auto-off time); after that the TV may rest.
    """
    if not viewed_at or wall_viewed_at > viewed_at:
        return False
    return now - viewed_at <= refresh_seconds + 90 and now - since <= keep_minutes * 60
