"""On-disk state for the feed wall.

- Cache (~/Library/Caches/lg-tv-feedwall/): each source's last good posts and when it last
  worked, plus downloaded images. A dead source keeps showing what it had.
- feedwall-state.json: the wall that's showing now (profile, end time) and schedule pauses.
- feedwall-decisions.json: approved/rejected posts for the approval queue.

The MCP server, the CLI and the page server all read and write these, so every
read-modify-write takes a file lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from ..tv import CONFIG_DIR

CACHE_DIR = Path(os.environ.get("LGTV_FEEDWALL_CACHE") or Path.home() / "Library/Caches/lg-tv-feedwall").expanduser()
STATE_FILE = CONFIG_DIR / "feedwall-state.json"
DECISIONS_FILE = CONFIG_DIR / "feedwall-decisions.json"
IMAGE_MAX_BYTES = 8_000_000
IMAGE_KEEP_DAYS = 7


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)


@contextlib.contextmanager
def locked_json(path: Path) -> Iterator[dict[str, Any]]:
    """Read a JSON file, let the caller change the dict, write it back, all under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        yield data
        _write(path, data)


def read_state() -> dict[str, Any]:
    return _read(STATE_FILE)


def read_decisions() -> dict[str, Any]:
    return _read(DECISIONS_FILE).get("posts", {})


# ---------------------------------------------------------------- per-source cache


def _source_path(source_id: str) -> Path:
    return CACHE_DIR / "sources" / f"{source_id}.json"


def load_source(source_id: str) -> dict[str, Any]:
    """{"posts": [...], "ok_at": unix time of the last successful fetch, "tried_at", "error", "meta"}."""
    return _read(_source_path(source_id))


def save_source(source_id: str, entry: dict[str, Any]) -> None:
    _write(_source_path(source_id), entry)


def source_ids_cached() -> list[str]:
    d = CACHE_DIR / "sources"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


# ---------------------------------------------------------------- images


def image_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def image_dir() -> Path:
    return CACHE_DIR / "images"


def register_images(urls: list[str]) -> None:
    """Remember which remote images the wall may show, so /wall/img/<key> isn't an open proxy."""
    urls = [u for u in urls if u and u.startswith("https://")]
    if not urls:
        return
    with locked_json(image_dir() / "index.json") as idx:
        for u in urls:
            idx[image_key(u)] = u


def image_url_for(key: str) -> str | None:
    return _read(image_dir() / "index.json").get(key)


def prune_images(now: float | None = None) -> int:
    now = now or time.time()
    d, n = image_dir(), 0
    if not d.exists():
        return 0
    for p in d.iterdir():
        if p.suffix != ".json" and p.is_file() and now - p.stat().st_mtime > IMAGE_KEEP_DAYS * 86400:
            p.unlink(missing_ok=True)
            n += 1
    return n
