"""`feedwall` command: now | run | stop | status | refresh | pending | approve | reject | pause | resume | install | uninstall."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import sys
from datetime import date, datetime

from ..briefing.cli import AGENTS, PROJECT_DIR, _uv
from ..briefing.config import load_config as load_briefing_config
from ..briefing.control import launchctl
from ..tv import TV, TVError
from . import control, schedule
from .config import CONFIG_FILE, LOG_FILE, ensure_config_file, load_config, setup_logging
from .control import AGENT_PREFIX
from .show import end_wall, run_scheduled, show_wall
from .wall import refresh

STDERR_LOG = LOG_FILE.with_suffix(".launchd.log")


async def _with_tv(coro_fn) -> int:
    log = setup_logging()
    tv = TV()
    try:
        print(await coro_fn(tv))
        return 0
    except (TVError, ValueError) as e:
        log.error("problem: %s", e)
        print(f"Problem: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected error: %s", e)
        print(f"Unexpected error: {e} (details in {LOG_FILE})")
        return 1
    finally:
        await tv.close()


def _now() -> datetime:
    return datetime.now(load_briefing_config().tz)


def cmd_now(a) -> int:
    setup_logging().info("show %s: started by hand", a.profile)
    return asyncio.run(_with_tv(lambda tv: show_wall(tv, a.profile, minutes=a.minutes, take_over=True if a.force else None)))


def cmd_run(a) -> int:
    setup_logging().info("run: scheduled %s wall started", a.profile)
    return asyncio.run(_with_tv(lambda tv: run_scheduled(tv, a.profile)))


def cmd_stop(a) -> int:
    return asyncio.run(_with_tv(lambda tv: end_wall(tv, "stopped from the command line", action="off" if a.off else None)))


def cmd_status(_a) -> int:
    print(json.dumps(control.status(load_config(), _now()), indent=2, ensure_ascii=False))
    return 0


def cmd_refresh(a) -> int:
    setup_logging()
    cfg = load_config()
    asyncio.run(refresh(cfg, cfg.profile(a.profile), force=True))
    for s in control.profile_status(cfg, a.profile, _now(), {})["sources"]:
        print(f"  {s['id']:<20} {s['kind']:<17} {s['posts']:>3} posts  {s['state']} (updated {s['last_updated']})")
    return 0


def cmd_pending(a) -> int:
    items = asyncio.run(control.pending(a.profile, a.limit))
    if not items:
        print("Nothing waiting for approval.")
    for it in items:
        print(f"[{it['id']}] {it['author']} · {it['posted'][:16]} · {it['source']}\n    {it['text'][:200]}\n    {it['link']}")
    return 0


def _decide(a, status: str) -> int:
    print(asyncio.run(control.moderate_posts(a.profile, status, a.ids, a.newest)))
    return 0


def cmd_pause(a) -> int:
    try:
        print(control.pause(a.profile, _now(), a.until, a.indefinitely))
    except ValueError as e:
        sys.exit(str(e))
    return 0


def cmd_resume(a) -> int:
    print(control.resume(a.profile, _now()))
    return 0


def _plist(label: str, profile: str, intervals: list[dict]) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [_uv(), "run", "--directory", str(PROJECT_DIR), "feedwall", "run", profile],
        "WorkingDirectory": str(PROJECT_DIR), "StandardOutPath": str(STDERR_LOG), "StandardErrorPath": str(STDERR_LOG),
        "ProcessType": "Interactive", "StartCalendarInterval": intervals,
    }


def cmd_install(_a) -> int:
    if ensure_config_file():
        print(f"Created settings file {CONFIG_FILE}")
    cfg = load_config()
    AGENTS.mkdir(parents=True, exist_ok=True)
    for name, p in cfg.profiles.items():
        label = AGENT_PREFIX + name
        path = AGENTS / f"{label}.plist"
        launchctl("bootout", f"gui/{os.getuid()}/{label}")
        hours = schedule.parse_hours(p.hours)
        if hours is None:
            if path.exists():
                path.unlink()
                print(f"Removed {path} ({name} has no show hours)")
            continue
        intervals = [{"Weekday": d % 7, "Hour": hours[0].hour, "Minute": hours[0].minute} for d in sorted(set(p.days))]
        with path.open("wb") as f:
            plistlib.dump(_plist(label, name, intervals), f)
        r = launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        if r.returncode:
            sys.exit(f"launchctl couldn't load {path}: {r.stderr.strip()}")
        days = ", ".join(schedule.DAY_NAMES[d - 1] for d in sorted(set(p.days)))
        print(f"{name}: shows {days} {p.hours}, then {p.at_end} ({path})")
    print("The wall is served by the briefing's page server; if it isn't installed yet, run `uv run briefing install`.")
    return 0


def cmd_uninstall(_a) -> int:
    for path in AGENTS.glob(f"{AGENT_PREFIX}*.plist"):
        launchctl("bootout", f"gui/{os.getuid()}/{path.stem}")
        path.unlink()
        print(f"Removed {path}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="feedwall", description="Feed wall for the LG TV")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("now", help="show a profile's wall now")
    n.add_argument("profile", nargs="?", default="personal")
    n.add_argument("--minutes", type=int, default=None, help="how long (0 = until stopped; default from the profile)")
    n.add_argument("--force", action="store_true", help="take over the screen even if something is playing")
    r = sub.add_parser("run", help="the scheduled start of a profile's show hours (used by launchd)")
    r.add_argument("profile")
    s = sub.add_parser("stop", help="take the wall off the TV")
    s.add_argument("--off", action="store_true", help="turn the TV off instead of going to the home screen")
    sub.add_parser("status", help="what's showing, source health, pending posts, schedules")
    rf = sub.add_parser("refresh", help="fetch a profile's sources now and show their health")
    rf.add_argument("profile", nargs="?", default="personal")
    pe = sub.add_parser("pending", help="posts waiting for approval")
    pe.add_argument("profile", nargs="?", default="organization")
    pe.add_argument("--limit", type=int, default=20)
    for name in ("approve", "reject"):
        x = sub.add_parser(name, help=f"{name} pending posts by id, or the newest N")
        x.add_argument("ids", nargs="*")
        x.add_argument("--newest", type=int, default=None, metavar="N")
        x.add_argument("--profile", default="organization")
    pa = sub.add_parser("pause", help="skip a profile's scheduled showings")
    pa.add_argument("profile")
    g = pa.add_mutually_exclusive_group(required=True)
    g.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD")
    g.add_argument("--indefinitely", action="store_true")
    re_ = sub.add_parser("resume", help="end a pause")
    re_.add_argument("profile")
    sub.add_parser("install", help="install launchd jobs for profiles with show hours")
    sub.add_parser("uninstall", help="remove the feed wall's launchd jobs")
    a = p.parse_args()
    handlers = {"now": cmd_now, "run": cmd_run, "stop": cmd_stop, "status": cmd_status, "refresh": cmd_refresh,
                "pending": cmd_pending, "approve": lambda a: _decide(a, "approved"), "reject": lambda a: _decide(a, "rejected"),
                "pause": cmd_pause, "resume": cmd_resume, "install": cmd_install, "uninstall": cmd_uninstall}
    try:
        sys.exit(handlers[a.cmd](a))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
