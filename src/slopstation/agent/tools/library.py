"""Game-library index builder.

Layer 1 (installed) comes from `ssh gamepc games`. Layers 2-3 (owned/playtime
via the Steam Web API, metadata via appdetails + SteamSpy) merge into the same
file. Layer 4 is live store data (deals, search, reviews, news,
how-long-to-beat), not the catalog; it lives in steamstore.py.

Output: state/library.json, written atomically. sync() runs on a background
thread at startup, every SYNC_S while the lane is up, and after each voice
session.

CLI:
    python -m slopstation.agent.tools.library sync                        (every layer, as the agent does)
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


# --- layer 1 sources ----------------------------------------------------------


def fetch_installed_ssh() -> list[dict]:
    """Production path (K15): the gaming PC enumerates its own ACFs."""
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
    """One read of the index for one voice session - the pipeline's vocabulary,
    resolvers and gate all see the same snapshot while the background sync
    may be rewriting the file. Per-operation readers (dispatch, the tools)
    keep reading the file."""

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
    """Collection name->id into the index. Fail-soft when the PC is asleep."""
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


# --- layers 2-3: owned/playtime + metadata ------------------------------------


def meta_cache_file():
    return paths.state("metadata-cache.json")


_CTRL = {28: "full", 18: "partial"}  # Steam category ids


def fetch_owned(api_key: str, steamid: str) -> dict:
    """Account-global playtime + recency (appmanifest LastPlayed is
    per-machine). One call, own key only."""
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
    """appdetails (genres/controller/desc/score/year) + SteamSpy tags. Caller
    paces the requests."""
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
    """Top up NEW appids only, ~1 req/2 s (appdetails' unofficial ceiling).
    Saves after EACH fetch: the daemon crawl thread dies with the agent, so
    batching would re-crawl from zero every restart. Cached forever."""
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
    """ASCII-only: encoding-proof across every hop, (tm) glyphs are noise."""
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def fuzzy_key(name: str | None) -> str:
    """Letters and digits only: rogue-like == Roguelike, 'co op' == Co-op,
    and the hltb-cache key."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def steam_creds() -> tuple[str, str] | None:
    """(steamApiKey, steamId64) when both are real, else None. Logs nothing:
    each caller decides whether a missing key is news."""
    s = config.secrets()
    key, sid = s.get("steamApiKey"), str(s.get("steamId64", ""))
    return (key, sid) if config.real_key(key) and sid.isdigit() else None


# --- the sync orchestrator (all layers, staleness- and key-gated) ------------
# Layer 1 needs the PC awake and fail-softs when asleep; layers 2-3 come from
# the Steam cloud, so the catalog stays current while the rig sleeps.
OWNED_MAX_AGE_S = 6 * 3600  # playtime/recency drift slowly; one call/6h
# The catalog a session speaks from is snapshotted at session OPEN, so a sync
# that runs at open is already too late for that conversation: the ceiling
# below is how stale a just-installed game can be when the user asks about it.
SYNC_S = 300
# Layer 1 is the only one that needs the PC, and it fail-softs with a
# sync_skipped per try. Holding off after one keeps a sleeping night at ~20
# of those instead of ~290, which is the difference between signal and noise.
SYNC_ASLEEP_S = 1800
_sync_lock = threading.Lock()


def _iso_age(index: dict, key: str) -> float | None:
    """Seconds since index[key] (iso timestamp), or None if absent/unparseable."""
    try:
        return time.time() - time.mktime(time.strptime(index[key], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        return None


def sync() -> bool | None:
    """Full catalog refresh for the background thread: installed every call,
    owned when stale >6h, metadata top-up for new appids. Steam layers are
    skipped without keys. Non-reentrant, so calls can't stack meta crawls.

    True when layer 1 refreshed, False when the PC was unreachable, None when
    another sync held the lock and this call did nothing. periodic_sync backs
    off on False only: a held lock says nothing about the PC."""
    if not _sync_lock.acquire(blocking=False):
        return None
    installed = False
    try:
        # Layer 1b only when layer 1 SUCCEEDED - both need the PC awake, so
        # gating spares a sleeping sync (and the blind test) a 15 s ssh wait.
        installed = refresh() == 0  # layer 1 (fail-softs asleep)
        if installed:
            refresh_collections()  # layer 1b (PC-dependent too)
        # Layer 4 is keyless, so it runs BEFORE the key gate.
        from slopstation.agent.tools import steamstore

        d_age = _iso_age(steamstore.load_deals(), "refreshed")
        if d_age is None or d_age > steamstore.DEALS_MAX_AGE_S:
            steamstore.refresh_deals()
        if steam_creds():  # without a key: layers 1+4 only
            age = _iso_age(load(), "ownedRefreshed")
            if age is None or age > OWNED_MAX_AGE_S:
                refresh_owned()  # layer 2
            index = load()
            appids = {r["appid"] for r in index.get("installed", [])}
            appids.update(int(a) for a in index.get("owned", {}))
            if any(str(a) not in load_meta() for a in appids):
                refresh_meta(list(appids))  # layer 3 (top-up only)
    except Exception as e:
        log.error("sync_failed", err=str(e))
    finally:
        _sync_lock.release()
    return installed


def periodic_sync():
    """Tick body for an events.Ticker on SYNC_S: sync, then hold off until
    SYNC_ASLEEP_S has passed instead when the PC was unreachable. Off the wake
    path entirely, so a session pays nothing for a fresh catalog."""
    held = {"until": 0.0}

    def tick() -> None:
        if time.monotonic() < held["until"]:
            return
        ran = sync()
        if ran is None:
            return  # a session-close sync holds the lock; retry next tick
        held["until"] = time.monotonic() + (SYNC_S if ran else SYNC_ASLEEP_S)

    return tick


def query_terms(limit: int | None = 30) -> list[str]:
    """Distinct tags/genres, frequency-ranked: the raw material for the STT
    vocabulary ("mech games" -> "met games"); None for the whole ranking.
    Ranking is all this owes - keyterms.query_keyterms picks the spoken
    form and drops the generic words."""
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
    installed_ids = {
        r["appid"] for r in index.get("installed", []) if r["appid"] not in NOT_GAMES
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
        lines.append(
            (
                appid in installed_ids,
                o.get("hours", 0),
                (
                    f"{appid}|{name}|{','.join(m.get('tags', [])[:5])}"
                    f"|{','.join(m.get('genres', [])[:3])}|{o.get('hours', 0)}h"
                    f"|{last}|{'inst' if appid in installed_ids else 'notinst'}"
                    f"|{m.get('controller', '?')}"
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
