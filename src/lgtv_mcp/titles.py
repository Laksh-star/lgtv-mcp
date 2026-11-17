"""Resolve a movie/series to streaming-app content IDs (Wikidata first), with a local cache.

Wikidata links TMDB IDs to the IDs streaming services use in their URLs:

  P4947 TMDB movie ID          P4983 TMDB TV series ID
  P1874 Netflix ID             (netflix.com/title/<id>, also playable as netflix.com/watch/<id>)
  P14462 Prime Video GTI       (amzn1.dv.gti.<uuid>; the only Prime id the LG app deep-links with)
  P11049 JioHotstar ID
  (P1651 YouTube video ID is skipped: on films it is usually a trailer.)

There is no ZEE5 property, and episodes rarely carry streaming IDs, so those usually come back as
"needs lookup" for the caller to find by web search and pass back in (then they are cached here).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp

from .tv import CONFIG_DIR

log = logging.getLogger("lgtv_mcp.titles")

CACHE_FILE = CONFIG_DIR / "title_ids.json"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "lgtv-mcp/0.1 (https://github.com/Laksh-star/lgtv-mcp; play_title resolver)"
CACHE_TTL = 30 * 24 * 3600  # re-check Wikidata monthly; manual entries never expire

TMDB_PROP = {"movie": "P4947", "tv": "P4983"}
SERVICE_PROPS = {  # service key -> Wikidata properties, best first
    "netflix": ["P1874"],
    "prime": ["P14462"],  # GTI only: the LG Prime app ignores P14440 detail ids and P8055 ASINs
    "hotstar": ["P11049"],
}


# ---------------------------------------------------------------- cache


def _load_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CACHE_FILE)


def cache_key(media_type: str, tmdb_id: int | None, title: str | None,
              season: int | None = None, episode: int | None = None) -> str:
    base = f"{media_type}:{tmdb_id}" if tmdb_id else f"{media_type}:title:{(title or '').strip().lower()}"
    if season is not None and episode is not None:
        base += f":s{season}e{episode}"
    return base


def remember(key: str, service: str, content_id: str, *, label: str | None = None) -> None:
    """Store an ID that was found some other way (e.g. a web search), so it is reused next time."""
    cache = _load_cache()
    entry = cache.setdefault(key, {"ids": {}, "sources": {}})
    entry["ids"][service] = content_id
    entry.setdefault("sources", {})[service] = "manual"
    if label and not entry.get("label"):
        entry["label"] = label
    entry.setdefault("fetched", time.time())
    _save_cache(cache)


# ---------------------------------------------------------------- wikidata


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, str],
                    headers: dict[str, str] | None = None) -> Any:
    """GET JSON, backing off politely when Wikidata rate-limits us (429/503 + Retry-After)."""
    for attempt in range(3):
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status in (429, 503) and attempt < 2:
                try:
                    wait = min(float(r.headers.get("Retry-After", "")), 10.0)
                except ValueError:
                    wait = 2.0 * (attempt + 1)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return await r.json(content_type=None)
    raise RuntimeError("unreachable")


def _claim_values(entity: dict[str, Any], prop: str) -> list[str]:
    out = []
    for c in entity.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, str):
            out.append(v)
    return out


def _label(entity: dict[str, Any]) -> str | None:
    labels = entity.get("labels", {})
    for lang in ("en", "mul", "en-gb", "ml", "hi", "ta", "te"):
        if lang in labels:
            return labels[lang]["value"]
    return next((v["value"] for v in labels.values()), None)


def _year(entity: dict[str, Any]) -> str | None:
    for prop in ("P577", "P580"):  # publication date, start time
        for c in entity.get("claims", {}).get(prop, []):
            t = c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time")
            if t:
                return t[1:5]
    return None


async def _search_items(session, query: str, limit: int = 5) -> list[str]:
    data = await _get_json(session, WIKIDATA_API, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": str(limit),
        "srnamespace": "0", "format": "json",
    })
    return [h["title"] for h in data.get("query", {}).get("search", [])]


async def _entities(session, qids: list[str]) -> dict[str, Any]:
    if not qids:
        return {}
    data = await _get_json(session, WIKIDATA_API, {
        "action": "wbgetentities", "ids": "|".join(qids), "props": "labels|claims",
        "languages": "en|mul|en-gb|ml|hi|ta|te", "format": "json",
    })
    return data.get("entities", {})


def _ids_from_entity(entity: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for service, props in SERVICE_PROPS.items():
        for prop in props:
            vals = _claim_values(entity, prop)
            if vals:
                ids[service] = vals[0]
                ids.setdefault(f"_{service}_prop", prop)
                break
    return ids


async def _episode_entity(session, series_tmdb: int, season: int, episode: int) -> str | None:
    """Find the Wikidata item for SxxEyy of a series (season item ordinal + episode ordinal)."""
    q = (
        "SELECT ?ep WHERE { ?series wdt:P4983 \"%d\" . "
        "?season p:P179 [ps:P179 ?series; pq:P1545 \"%d\"] . "
        "?ep p:P4908 [ps:P4908 ?season; pq:P1545 \"%d\"] . } LIMIT 1"
    ) % (series_tmdb, season, episode)
    data = await _get_json(session, WIKIDATA_SPARQL, {"query": q},
                           headers={"Accept": "application/sparql-results+json"})
    rows = data["results"]["bindings"]
    return rows[0]["ep"]["value"].rsplit("/", 1)[-1] if rows else None


async def resolve(*, title: str | None, tmdb_id: int | None, media_type: str = "movie",
                  season: int | None = None, episode: int | None = None,
                  refresh: bool = False) -> dict[str, Any]:
    """Return {"key", "label", "wikidata", "tmdb_id", "ids": {service: id}, "sources", "candidates"?}.

    For an episode, "ids" holds episode-level IDs (rarely on Wikidata) and "series_ids" the
    series-level ones, which apps usually treat as "resume / start the show".
    """
    key = cache_key(media_type, tmdb_id, title, season, episode)
    cache = _load_cache()
    hit = cache.get(key)
    if hit and not refresh:
        fresh = time.time() - hit.get("fetched", 0) < CACHE_TTL
        if fresh or "manual" in hit.get("sources", {}).values():
            return {"key": key, "cached": True, **hit}

    result: dict[str, Any] = {"key": key, "ids": {}, "sources": {}, "tmdb_id": tmdb_id}
    episode_mode = media_type == "tv" and season is not None and episode is not None
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        prop = TMDB_PROP[media_type]
        if tmdb_id:
            qids = await _search_items(session, f"haswbstatement:{prop}={tmdb_id}", 2)
        elif title:
            qids = await _search_items(session, f"{title} haswbstatement:{prop}", 5)
        else:
            raise ValueError("Give a title or a TMDB id.")
        ents = await _entities(session, qids)
        ents_ordered = [ents[q] for q in qids if q in ents and "missing" not in ents[q]]
        if not ents_ordered:
            result["wikidata"] = None
        else:
            main = ents_ordered[0]
            result["wikidata"] = main["id"]
            result["label"] = _label(main)
            result["year"] = _year(main)
            if not tmdb_id:
                tm = _claim_values(main, prop)
                result["tmdb_id"] = int(tm[0]) if tm and tm[0].isdigit() else None
                if len(ents_ordered) > 1:
                    result["candidates"] = [
                        {"wikidata": e["id"], "label": _label(e), "year": _year(e),
                         "tmdb_id": (_claim_values(e, prop) or [None])[0]}
                        for e in ents_ordered
                    ]
            series_ids = _ids_from_entity(main)
            if episode_mode:
                result["series_ids"] = {k: v for k, v in series_ids.items() if not k.startswith("_")}
                series_tmdb = result.get("tmdb_id")
                ep_q = await _episode_entity(session, int(series_tmdb), season, episode) if series_tmdb else None
                result["episode_wikidata"] = ep_q
                if ep_q:
                    ep_ent = (await _entities(session, [ep_q])).get(ep_q, {})
                    result["episode_label"] = _label(ep_ent)
                    found = _ids_from_entity(ep_ent)
                else:
                    found = {}
            else:
                found = series_ids
            for k, v in found.items():
                if not k.startswith("_"):
                    result["ids"][k] = v
                    result["sources"][k] = f"wikidata:{found.get(f'_{k}_prop')}"

    # Keep manual entries (found by web search earlier) on refresh.
    if hit:
        for svc, src in hit.get("sources", {}).items():
            if src == "manual" and svc not in result["ids"]:
                result["ids"][svc] = hit["ids"][svc]
                result["sources"][svc] = "manual"
    result["fetched"] = time.time()
    if result.get("wikidata") or result["ids"]:
        cache[key] = {k: v for k, v in result.items() if k not in ("key", "candidates")}
        _save_cache(cache)
    return result
