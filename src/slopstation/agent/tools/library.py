"""Build the game-library index from the gaming PC and Steam APIs.

Installed games, collections, owned games, playtime, and metadata are stored
in state/library.json. Live store data is handled by steamstore.py. ``sync()``
runs at startup, after each voice session, and periodically while the service
is running.

CLI:
    python -m slopstation.agent.tools.library sync
    python -m slopstation.agent.tools.library refresh [--owned] [--meta [N]]
    python -m slopstation.agent.tools.library show
    python -m slopstation.agent.tools.library catalog
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time

from slopstation import config, logbook, paths, statefile


def library_file():
    return paths.state("library.json")


log = logbook.logger("library")


# --- Gaming PC data -----------------------------------------------------------


def fetch_installed_ssh() -> list[dict]:
    """Return the games installed on the gaming PC."""
    from slopstation import gamepc

    return parse_games_json(gamepc.games())


def parse_games_json(text: str) -> list[dict]:
    rows = json.loads(text.strip().lstrip("﻿"))
    if isinstance(rows, dict):  # single-game library edge
        rows = [rows]
    return rows


# --- index file ---------------------------------------------------------------


def load() -> dict:
    return statefile.load(library_file(), {})


def installed_name(appid: int) -> str | None:
    """Installed title for an appid, or None."""
    for r in load().get("installed", []):
        if r["appid"] == appid:
            return r["name"]
    return None


class Catalog:
    """A consistent library snapshot for one voice session."""

    def __init__(self, index: dict) -> None:
        self.installed = index.get("installed", [])
        self.collections = index.get("collections", [])

    @classmethod
    def load(cls) -> Catalog:
        return cls(load())


def save(index: dict) -> None:
    statefile.write(library_file(), index)


def refresh() -> int:
    try:
        rows = fetch_installed_ssh()
    except Exception as e:
        log.warn("sync_skipped", layer="installed", err=str(e))
        return 1
    index = load()
    index["refreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    index["installed"] = rows
    save(index)
    log("sync_done", layer="installed", games=len(rows))
    return 0


def fetch_collections_ssh() -> list[dict]:
    """Big Picture collections as [{name, id}]. Needs the PC awake."""
    from slopstation import gamepc

    return parse_games_json(gamepc.collections())


def refresh_collections() -> int:
    """Refresh collection names and IDs when the gaming PC is reachable."""
    try:
        rows = fetch_collections_ssh()
    except Exception as e:
        log.warn("sync_skipped", layer="collections", err=str(e))
        return 1
    index = load()
    index["collections"] = rows
    save(index)
    log("sync_done", layer="collections", n=len(rows))
    return 0


def show() -> int:
    index = load()
    rows = sorted(
        index.get("installed", []), key=lambda r: r.get("lastPlayed", 0), reverse=True
    )
    if not rows:
        print("no index - run: python -m slopstation.agent.tools.library refresh")
        return 1
    print(f"refreshed {index.get('refreshed', '?')} - {len(rows)} installed")
    for r in rows:
        last = (
            time.strftime("%Y-%m-%d", time.localtime(r["lastPlayed"]))
            if r.get("lastPlayed")
            else "never"
        )
        print(f"  {r['appid']:>8}  {last}  {r['name']}")
    return 0


# --- Steam account and metadata -----------------------------------------------


def meta_cache_file():
    return paths.state("metadata-cache.json")


_CTRL = {28: "full", 18: "partial"}  # Steam category ids


def fetch_owned(api_key: str, steamid: str) -> dict:
    """Return account-wide playtime and recency data."""
    import requests

    r = requests.get(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
        params={
            "key": api_key,
            "steamid": steamid,
            "include_appinfo": "1",
            "include_played_free_games": "1",
            "format": "json",
        },
        timeout=30,
    )
    r.raise_for_status()
    out = {}
    for g in r.json().get("response", {}).get("games", []):
        out[str(g["appid"])] = {
            "hours": round(g.get("playtime_forever", 0) / 60, 1),
            "hours2w": round(g.get("playtime_2weeks", 0) / 60, 1),
            "last": g.get("rtime_last_played", 0),
            "name": ascii_only(g.get("name", "")),
        }
    return out


def fetch_meta_one(appid: int) -> dict:
    """Fetch store metadata and SteamSpy tags for one game."""
    import requests

    meta = {}
    r = requests.get(
        "https://store.steampowered.com/api/appdetails",
        params={"appids": appid},
        timeout=20,
    )
    d = r.json().get(str(appid), {})
    if d.get("success"):
        data = d["data"]
        cats = {c["id"] for c in data.get("categories", [])}
        meta.update(
            {
                "genres": [g["description"] for g in data.get("genres", [])],
                "controller": next((v for k, v in _CTRL.items() if k in cats), "none"),
                "desc": ascii_only(data.get("short_description", ""))[:160],
                "score": (data.get("metacritic") or {}).get("score"),
                "year": (data.get("release_date") or {}).get("date", "")[-4:],
            }
        )
    r2 = requests.get(
        "https://steamspy.com/api.php",
        params={"request": "appdetails", "appid": str(appid)},
        timeout=20,
    )
    tags = r2.json().get("tags") or {}
    if isinstance(tags, dict):
        meta["tags"] = [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:10]]
    return meta


def load_meta() -> dict:
    return statefile.load(meta_cache_file(), {})


def _save_meta(cache: dict) -> None:
    statefile.write(meta_cache_file(), cache)


def refresh_meta(appids: list[int], limit: int = 200) -> dict:
    """Fetch uncached metadata at about one request every two seconds.

    Save each result immediately so an interrupted refresh can resume.
    """
    cache = load_meta()
    todo = [a for a in appids if str(a) not in cache][:limit]
    for i, appid in enumerate(todo):
        try:
            cache[str(appid)] = fetch_meta_one(appid)
            _save_meta(cache)
            log("meta_fetched", appid=appid, n=i + 1, of=len(todo))
        except Exception as e:
            log.warn("meta_failed", appid=appid, err=str(e))
        if i < len(todo) - 1:
            time.sleep(2.1)  # the rate limit is between items, not after the last
    return cache


def refresh_owned() -> int:
    creds = steam_creds()
    if not creds:
        log("sync_skipped", layer="owned", reason="steamApiKey/steamId64 not set")
        return 1
    index = load()
    index["owned"] = fetch_owned(*creds)
    index["ownedRefreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save(index)
    log("sync_done", layer="owned", games=len(index["owned"]))
    return 0


# --- shared by the catalog and steamstore -------------------------------------
NOT_GAMES = {228980}  # Steamworks Common Redistributables


def ascii_only(s: str | None) -> str:
    """Strip non-ASCII characters from a display string."""
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def fuzzy_key(name: str | None) -> str:
    """Normalize a name to lowercase ASCII letters and digits."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def steam_creds() -> tuple[str, str] | None:
    """Return the configured Steam API key and account ID, if valid."""
    s = config.secrets()
    key, sid = s.get("steamApiKey"), str(s.get("steamId64", ""))
    return (key, sid) if config.real_key(key) and sid.isdigit() else None


# --- Background refresh -------------------------------------------------------
OWNED_MAX_AGE_S = 6 * 3600
# Maximum age of installed-game data while the service is running.
SYNC_S = 300
# Wait longer after the gaming PC is unavailable.
SYNC_ASLEEP_S = 1800
_sync_lock = threading.Lock()


def _iso_age(index: dict, key: str) -> float | None:
    """Seconds since index[key] (iso timestamp), or None if absent/unparseable."""
    try:
        return time.time() - time.mktime(time.strptime(index[key], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        return None


def sync() -> bool | None:
    """Refresh available library data without overlapping another refresh.

    Return whether installed games refreshed, or ``None`` if no result was
    obtained.
    """
    if not _sync_lock.acquire(blocking=False):
        return None
    installed: bool | None = None
    try:
        # Both calls need the gaming PC, so skip the second SSH timeout if the
        # first call fails.
        installed = refresh() == 0
        if installed:
            refresh_collections()
        from slopstation.agent.tools import steamstore

        d_age = _iso_age(steamstore.load_deals(), "refreshed")
        if d_age is None or d_age > steamstore.DEALS_MAX_AGE_S:
            steamstore.refresh_deals()
        if steam_creds():
            age = _iso_age(load(), "ownedRefreshed")
            if age is None or age > OWNED_MAX_AGE_S:
                refresh_owned()
            index = load()
            appids = {r["appid"] for r in index.get("installed", [])}
            appids.update(int(a) for a in index.get("owned", {}))
            if any(str(a) not in load_meta() for a in appids):
                refresh_meta(list(appids))
    except Exception as e:
        log.error("sync_failed", err=str(e))
    finally:
        _sync_lock.release()
    return installed


def periodic_sync():
    """Return a refresh callback that waits longer after an unreachable PC."""
    held = {"until": 0.0}

    def tick() -> None:
        if time.monotonic() < held["until"]:
            return
        if sync() is False:  # None means another refresh owns the lock.
            held["until"] = time.monotonic() + SYNC_ASLEEP_S

    return tick


def query_terms(limit: int | None = 30) -> list[str]:
    """Return tags and genres ranked by frequency."""
    counts: dict[str, int] = {}
    for m in load_meta().values():
        for term in (m.get("tags") or []) + (m.get("genres") or []):
            t = term.lower()
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts, key=lambda t: -counts[t])[:limit]


def catalog_lines() -> list[str]:
    """Compact rows for the assistant's context, installed first:
    appid|name|tags|genres|hours|lastPlayed|installed|ctrl"""
    index = load()
    meta = load_meta()
    owned = {k: v for k, v in index.get("owned", {}).items() if int(k) not in NOT_GAMES}
    # Map installed app IDs to their last update time.
    installed_at = {
        r["appid"]: r.get("updated", 0)
        for r in index.get("installed", [])
        if r["appid"] not in NOT_GAMES
    }
    rows = {
        r["appid"]: r["name"]
        for r in index.get("installed", [])
        if r["appid"] not in NOT_GAMES
    }
    for appid, o in owned.items():
        rows.setdefault(int(appid), o.get("name") or f"app {appid}")
    # Playtest/beta stubs have no metadata and pollute recommendations.
    rows = {a: n for a, n in rows.items() if not n.endswith(" Playtest")}
    lines = []
    for appid, name in rows.items():
        o = owned.get(str(appid), {})
        m = meta.get(str(appid), {})
        # Day precision, so the model can say what was played last.
        last = (
            time.strftime("%Y-%m-%d", time.localtime(o["last"]))
            if o.get("last")
            else "never"
        )
        # Include the update date when Steam provides it.
        if appid not in installed_at:
            inst = "notinst"
        elif installed_at[appid]:
            inst = "inst:" + time.strftime(
                "%Y-%m-%d", time.localtime(installed_at[appid])
            )
        else:
            inst = "inst"
        lines.append(
            (
                appid in installed_at,
                o.get("hours", 0),
                (
                    f"{appid}|{name}|{','.join(m.get('tags', [])[:5])}"
                    f"|{','.join(m.get('genres', [])[:3])}|{o.get('hours', 0)}h"
                    f"|{last}|{inst}|{m.get('controller', '?')}"
                ),
            )
        )
    lines.sort(key=lambda t: (not t[0], -t[1]))
    return [line for _, _, line in lines]


def catalog() -> int:
    lines = catalog_lines()
    text = "\n".join(lines)
    print(text)
    print(f"\n# {len(lines)} games, ~{len(text) // 4} tokens", file=sys.stderr)
    return 0


def usage() -> int:
    print(
        "usage: python -m slopstation.agent.tools.library sync | refresh [--owned] "
        "[--meta [N]] | show | catalog"
    )
    return 2


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["sync"]:
        sync()
        sys.exit(0)
    elif args[:1] == ["refresh"]:
        rc = refresh()
        if "--owned" in args:
            rc = refresh_owned() or rc
        if "--meta" in args:
            i = args.index("--meta")
            n = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else 200
            refresh_meta([r["appid"] for r in load().get("installed", [])], n)
        sys.exit(rc)
    elif args[:1] == ["show"]:
        sys.exit(show())
    elif args[:1] == ["catalog"]:
        sys.exit(catalog())
    else:
        sys.exit(usage())
