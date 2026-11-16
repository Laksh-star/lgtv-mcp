"""Add (or update) the lgtv server in the Claude desktop app's config.

Run it while the Claude desktop app is fully quit (Cmd+Q). The app keeps its config in
memory and saves it back to disk while it runs, which wipes out edits made in the meantime.

    /opt/homebrew/bin/uv run --directory /path/to/lg-tv python scripts/install_claude_desktop.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
PROJECT_DIR = Path(__file__).resolve().parents[1]
APP_BINARY = "/Applications/Claude.app/Contents/MacOS/Claude"


def find_uv() -> str:
    for candidate in (shutil.which("uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv",
                      str(Path.home() / ".local/bin/uv"), str(Path.home() / ".cargo/bin/uv")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            # Keep the stable symlink (e.g. /opt/homebrew/bin/uv); resolving it would pin a
            # versioned Cellar path that disappears on the next `brew upgrade`.
            return str(Path(candidate).absolute())
    sys.exit("Couldn't find uv. Install it (brew install uv) and run this again.")


def desktop_app_running() -> bool:
    out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout
    return any(line.strip() == APP_BINARY for line in out.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--force", action="store_true", help="write even if the desktop app is running")
    args = ap.parse_args()

    if not args.force and args.config == DEFAULT_CONFIG and desktop_app_running():
        sys.exit("The Claude desktop app is still running, and it would overwrite this change.\n"
                 "Quit it fully with Cmd+Q, then run this command again.")

    config: dict = {}
    if args.config.exists():
        raw = args.config.read_text()
        try:
            config = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            sys.exit(f"{args.config} isn't valid JSON ({e}). Fix or restore it first; nothing was changed.")
        backup = args.config.with_name(f"{args.config.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(args.config, backup)
        print(f"Backup: {backup}")

    entry = {"command": find_uv(), "args": ["run", "--directory", str(PROJECT_DIR), "lgtv-mcp"]}
    servers = config.setdefault("mcpServers", {})
    action = "Updated" if "lgtv" in servers else "Added"
    servers["lgtv"] = entry

    args.config.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.config.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(args.config)
    json.loads(args.config.read_text())  # re-validate what was written

    print(f"{action} lgtv: {entry['command']} {' '.join(entry['args'])}")
    print(f"Servers in config: {', '.join(servers)}")
    print("Now open the Claude desktop app again.")


if __name__ == "__main__":
    main()
