"""Briefing settings (~/.config/lgtv/briefing.toml), Keychain access and logging."""

from __future__ import annotations

import logging
import logging.handlers
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from ..tv import CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "briefing.toml"
LOG_FILE = Path.home() / "Library/Logs/lg-tv-briefing.log"
KEYCHAIN_SERVICE = "lgtv-briefing"
KEYCHAIN_ACCOUNT = "google-calendar-ics"

DEFAULT_TOML = """\
# Morning briefing settings. After changing [schedule], run:  uv run briefing install
# Everything else is picked up on the next page refresh (within 5 minutes).

[location]
name = "Hyderabad"
latitude = 17.385
longitude = 78.4867
timezone = "Asia/Kolkata"

[schedule]
weekdays = [1, 2, 3, 4, 5]   # 1 = Monday ... 7 = Sunday
time = "07:30"
auto_off_minutes = 30        # turn the TV off if the briefing is still showing; 0 = never

[calendar]
# The Google Calendar secret iCal address is read from the macOS login Keychain, never a file.
keychain_service = "lgtv-briefing"
keychain_account = "google-calendar-ics"   # "" = any account

[server]
port = 8765
allow_ips = []               # extra IPs allowed to open the page (the TV and this Mac always can)

[[feeds]]
name = "India"
url = "https://www.thehindu.com/news/national/feeder/default.rss"
count = 3

[[feeds]]
name = "World"
url = "https://feeds.bbci.co.uk/news/world/rss.xml"
count = 3
"""


@dataclass
class Feed:
    name: str
    url: str
    count: int = 3


@dataclass
class Config:
    city: str = "Hyderabad"
    latitude: float = 17.385
    longitude: float = 78.4867
    timezone: str = "Asia/Kolkata"
    weekdays: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    time: str = "07:30"
    auto_off_minutes: int = 30
    keychain_service: str = KEYCHAIN_SERVICE
    keychain_account: str = KEYCHAIN_ACCOUNT
    port: int = 8765
    allow_ips: list[str] = field(default_factory=list)
    feeds: list[Feed] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def hour_minute(self) -> tuple[int, int]:
        h, m = self.time.split(":")
        return int(h), int(m)


def ensure_config_file() -> bool:
    """Write the default settings file if there isn't one. Returns True if it was created."""
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(DEFAULT_TOML)
    return True


def load_config() -> Config:
    raw = tomllib.loads(CONFIG_FILE.read_text() if CONFIG_FILE.exists() else DEFAULT_TOML)
    loc, sched, srv = raw.get("location", {}), raw.get("schedule", {}), raw.get("server", {})
    cal = raw.get("calendar", {})
    cfg = Config(
        city=loc.get("name", "Hyderabad"),
        latitude=float(loc.get("latitude", 17.385)),
        longitude=float(loc.get("longitude", 78.4867)),
        timezone=loc.get("timezone", "Asia/Kolkata"),
        weekdays=[int(d) for d in sched.get("weekdays", [1, 2, 3, 4, 5])],
        time=str(sched.get("time", "07:30")),
        auto_off_minutes=int(sched.get("auto_off_minutes", 30)),
        keychain_service=str(cal.get("keychain_service", KEYCHAIN_SERVICE)),
        keychain_account=str(cal.get("keychain_account", KEYCHAIN_ACCOUNT)),
        port=int(srv.get("port", 8765)),
        allow_ips=list(srv.get("allow_ips", [])),
        feeds=[Feed(f["name"], f["url"], int(f.get("count", 3))) for f in raw.get("feeds", [])],
    )
    cfg.hour_minute  # validate "HH:MM" early
    cfg.tz
    return cfg


def calendar_url(cfg: Config | None = None) -> str | None:
    """The Google Calendar secret iCal address, from the login Keychain (never from a file)."""
    cfg = cfg or load_config()
    cmd = ["security", "find-generic-password", "-s", cfg.keychain_service]
    if cfg.keychain_account:
        cmd += ["-a", cfg.keychain_account]
    r = subprocess.run([*cmd, "-w"], capture_output=True, text=True, check=False, timeout=90)
    url = r.stdout.strip()
    return url or None


def setup_logging() -> logging.Logger:
    log = logging.getLogger("briefing")
    if not log.handlers:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False  # keep the MCP server's stderr quiet; everything goes to the log file
    return log
