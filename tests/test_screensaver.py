"""When the page server keeps the TV's screensaver off the briefing."""

from lgtv_mcp.screensaver import briefing_on_screen

NOW = 1_800_000_000.0
REFRESH = 300  # the briefing page reloads itself every 5 minutes


def on(viewed_ago, since_ago, wall_ago=None, keep=30):
    wall = NOW - wall_ago if wall_ago is not None else 0.0
    return briefing_on_screen(NOW, NOW - viewed_ago, NOW - since_ago, wall, REFRESH, keep)


def test_kept_up_while_reloading_within_its_auto_off_time():
    assert on(viewed_ago=60, since_ago=10 * 60)
    assert on(viewed_ago=REFRESH + 80, since_ago=29 * 60)  # a reload is a little late


def test_left_alone_once_it_stops_reloading():
    assert not on(viewed_ago=REFRESH + 100, since_ago=10 * 60)


def test_left_alone_after_its_auto_off_time():
    assert not on(viewed_ago=60, since_ago=31 * 60)
    assert on(viewed_ago=60, since_ago=50 * 60, keep=60)


def test_the_feed_wall_took_over():
    assert not on(viewed_ago=120, since_ago=10 * 60, wall_ago=30)
    assert on(viewed_ago=30, since_ago=10 * 60, wall_ago=120)  # the briefing came back after the wall


def test_never_shown():
    assert not briefing_on_screen(NOW, 0.0, 0.0, 0.0, REFRESH, 30)
