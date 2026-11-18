"""`briefing` command: now | run | pause | resume | restart | serve | preview | install | uninstall | status."""

from __future__ import annotations

import argparse
import asyncio
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from ..tv import TV, TVError, TVUnavailable
from . import control
from .config import CONFIG_FILE, LOG_FILE, calendar_url, ensure_config_file, load_config, setup_logging
from .control import RUN_LABEL, SERVER_LABEL
from .show import show

PROJECT_DIR = Path(__file__).resolve().parents[3]
AGENTS = Path.home() / "Library/LaunchAgents"
STDERR_LOG = Path.home() / "Library/Logs/lg-tv-briefing.launchd.log"
PMSET_DAYS = "MTWRFSU"  # pmset weekday letters, Monday first
RETRY_AFTER = 120  # seconds before the scheduled run tries once more when the TV couldn't be reached
EXIT_TV_UNREACHABLE = 2


def _uv() -> str:
    for c in (shutil.which("uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv", str(Path.home() / ".local/bin/uv")):
        if c and os.access(c, os.X_OK):
            return str(Path(c).absolute())
    sys.exit("Couldn't find uv.")


async def _show(take_over: bool, auto_off: int) -> int:
    log = setup_logging()
    cfg = load_config()
    tv = TV()
    try:
        msg = await show(tv, cfg, take_over=take_over, auto_off_minutes=auto_off)
        print(msg)
        return 0
    except TVUnavailable as e:
        log.error("run: TV problem: %s", e)
        print(f"TV problem: {e}")
        return EXIT_TV_UNREACHABLE
    except TVError as e:
        log.error("run: TV problem: %s", e)
        print(f"TV problem: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - log anything unexpected rather than dying silently
        log.exception("run: unexpected error: %s", e)
        print(f"Unexpected error: {e} (details in {LOG_FILE})")
        return 1
    finally:
        await tv.close()


def cmd_now(a) -> int:
    setup_logging().info("run: started by hand (briefing now%s)", " --force" if a.force else "")
    return asyncio.run(_show(take_over=a.force, auto_off=a.auto_off))


def cmd_run(_a) -> int:
    log = setup_logging()
    cfg = load_config()
    if datetime.now(cfg.tz).isoweekday() not in cfg.weekdays:
        log.info("run: today isn't a briefing day, skipping")
        return 0
    if until := control.paused_through(datetime.now(cfg.tz).date()):
        log.info("run: briefing paused %s, skipping",
                 "until resumed" if until == control.FOREVER else f"through {until:%Y-%m-%d}")
        return 0
    log.info("run: scheduled briefing started")
    rc = asyncio.run(_show(take_over=False, auto_off=cfg.auto_off_minutes))
    if rc == EXIT_TV_UNREACHABLE:
        log.info("run: trying once more in %d s", RETRY_AFTER)
        time.sleep(RETRY_AFTER)
        log.info("run: second attempt started")
        rc = asyncio.run(_show(take_over=False, auto_off=cfg.auto_off_minutes))
        if rc != 0:
            log.error("run: gave up for today; the briefing didn't reach the TV")
    return rc


def cmd_pause(a) -> int:
    cfg = load_config()
    now = datetime.now(cfg.tz)
    try:
        msg = control.pause(cfg, now, briefings=a.days, until=a.until, indefinitely=a.indefinitely)
    except ValueError as e:
        sys.exit(str(e))
    setup_logging().info("pause: %s (from the command line)", msg)
    print(msg)
    return 0


def cmd_resume(_a) -> int:
    cfg = load_config()
    msg = control.resume(cfg, datetime.now(cfg.tz))
    setup_logging().info("resume: %s (from the command line)", msg)
    print(msg)
    return 0


def cmd_restart(_a) -> int:
    try:
        print(control.restart_server())
    except RuntimeError as e:
        sys.exit(str(e))
    return 0


def cmd_serve(_a) -> int:
    setup_logging()
    from .web import serve
    serve()
    return 0


def cmd_preview(a) -> int:
    from .page import render
    from .sources import gather
    setup_logging()
    cfg = load_config()
    b = asyncio.run(gather(cfg))
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(b, cfg.timezone))
    print(f"Wrote {out}")
    for k, v in b.errors.items():
        print(f"  {k} failed: {v}")
    return 0


def _plist(label: str, args: list[str], **extra) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [_uv(), "run", "--directory", str(PROJECT_DIR), "briefing", *args],
        "WorkingDirectory": str(PROJECT_DIR),
        "StandardOutPath": str(STDERR_LOG),
        "StandardErrorPath": str(STDERR_LOG),
        "ProcessType": "Interactive",
        **extra,
    }


_launchctl = control.launchctl


def _load(label: str, data: dict) -> None:
    path = AGENTS / f"{label}.plist"
    AGENTS.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(data, f)
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if r.returncode != 0:
        sys.exit(f"launchctl couldn't load {path}: {r.stderr.strip()}")
    print(f"Loaded {path}")


def _pmset_command(cfg) -> str:
    h, m = cfg.hour_minute
    wake = (datetime(2000, 1, 3, h, m) - timedelta(minutes=5)).strftime("%H:%M:%S")
    days = "".join(PMSET_DAYS[d - 1] for d in sorted(set(cfg.weekdays)))
    return f"sudo pmset repeat wakeorpoweron {days} {wake}"


def cmd_install(_a) -> int:
    if ensure_config_file():
        print(f"Created settings file {CONFIG_FILE}")
    cfg = load_config()
    h, m = cfg.hour_minute
    _load(SERVER_LABEL, _plist(SERVER_LABEL, ["serve"], RunAtLoad=True, KeepAlive=True))
    _load(RUN_LABEL, _plist(RUN_LABEL, ["run"], StartCalendarInterval=[
        {"Weekday": d % 7, "Hour": h, "Minute": m} for d in sorted(set(cfg.weekdays))]))
    days = ", ".join("Mon Tue Wed Thu Fri Sat Sun".split()[d - 1] for d in sorted(set(cfg.weekdays)))
    print(f"Briefing scheduled: {days} at {cfg.time}. Page server on port {cfg.port}.")
    print(f"To wake the Mac 5 minutes before (needs your password):\n  {_pmset_command(cfg)}")
    return 0


def cmd_uninstall(_a) -> int:
    for label in (RUN_LABEL, SERVER_LABEL):
        _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        p = AGENTS / f"{label}.plist"
        if p.exists():
            p.unlink()
            print(f"Removed {p}")
    print("Briefing disabled. To drop the Mac wake schedule too:  sudo pmset repeat cancel")
    return 0


def cmd_status(_a) -> int:
    cfg = load_config()
    st = control.status(cfg, datetime.now(cfg.tz))
    print(f"{SERVER_LABEL}: {st['page_server']}")
    print(f"{RUN_LABEL}: {st['scheduler']}")
    print(f"Schedule: {st['schedule']}")
    print(f"Paused: {st['paused'] or 'no'}")
    print(f"Next briefing: {st['next_briefing']}")
    print(f"Calendar address in Keychain ({cfg.keychain_service}): {'yes' if calendar_url(cfg) else 'NO (see README)'}")
    print(f"Settings: {CONFIG_FILE}{'' if CONFIG_FILE.exists() else ' (not created yet, using defaults)'}")
    sched = subprocess.run(["pmset", "-g", "sched"], capture_output=True, text=True).stdout.strip()
    print("Mac wake schedule: " + (sched.replace("\n", " | ") if sched else "none"))
    if LOG_FILE.exists():
        print(f"Last log lines ({LOG_FILE}):")
        print("".join(LOG_FILE.read_text().splitlines(keepends=True)[-6:]).rstrip())
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="briefing", description="Morning briefing for the LG TV")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("now", help="show the briefing on the TV now (same rules as the scheduled run)")
    n.add_argument("--force", action="store_true", help="take over the screen even if something is playing")
    n.add_argument("--auto-off", type=int, default=0, metavar="MIN", help="turn the TV off after MIN minutes if still showing")
    sub.add_parser("run", help="the scheduled run (used by launchd)")
    ps = sub.add_parser("pause", help="skip upcoming scheduled briefings")
    g = ps.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=1, metavar="N", help="skip the next N scheduled briefings (default 1)")
    g.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD", help="skip every briefing through this date")
    g.add_argument("--indefinitely", action="store_true", help="skip them all until `briefing resume`")
    sub.add_parser("resume", help="end a pause")
    sub.add_parser("restart", help="restart the page server")
    sub.add_parser("serve", help="run the page server (used by launchd)")
    pv = sub.add_parser("preview", help="render the page to a local HTML file")
    pv.add_argument("--out", default="~/Library/Caches/lg-tv-briefing/preview.html")
    sub.add_parser("install", help="install/refresh the launchd jobs from briefing.toml")
    sub.add_parser("uninstall", help="remove the launchd jobs (disables the briefing)")
    sub.add_parser("status", help="show what's installed and recent log lines")
    a = p.parse_args()
    handlers = {"now": cmd_now, "run": cmd_run, "pause": cmd_pause, "resume": cmd_resume, "restart": cmd_restart,
                "serve": cmd_serve, "preview": cmd_preview,
                "install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}
    sys.exit(handlers[a.cmd](a))


if __name__ == "__main__":
    main()
