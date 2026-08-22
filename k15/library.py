"""Game-library index builder.

Layer 1 (installed) comes from `ssh gamepc games`, or --local-steam (ACF scan)
when run on the gaming PC. Layers 2-3 (owned/playtime via the Steam Web API,
metadata via appdetails + SteamSpy) merge into the same file. Layer 4 is live
store data (deals, search, reviews, news, how-long-to-beat), not the catalog.

Layer 4 lives in steamstore.py; `probe` below delegates to it.

Output: state/library.json, written atomically. sync() runs on a background
thread at startup and after each voice session.

CLI:
    python library.py sync                        (every layer, as the agent does)
    python library.py refresh [--local-steam] [--owned] [--meta [N]]
    python library.py show
    python library.py catalog
    python library.py probe <deals|search ...|reviews <appid>|news <appid>
                             |hltb <name>|trending|recent>
"""
import glob
import json
import re
import sys
import threading
import time
from pathlib import Path

import cglib

STATE = cglib.STATE
LIBRARY = STATE / "library.json"

log = cglib.make_log("library")


# --- layer 1 sources ----------------------------------------------------------


def fetch_installed_ssh():
    """Production path (K15): the gaming PC enumerates its own ACFs."""
    import gamepc
    return parse_games_json(gamepc.games())


def parse_games_json(text):
    rows = json.loads(text.strip().lstrip("﻿"))
    if isinstance(rows, dict):          # single-game library edge
        rows = [rows]
    return rows


def fetch_installed_local():
    """Running ON the gaming PC: same fields as the Dispatch `games` verb."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
        steam = winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    roots = {steam.lower()}
    lf = Path(steam) / "steamapps" / "libraryfolders.vdf"
    if lf.exists():
        for m in re.finditer(r'^\s*"path"\s+"(.+)"\s*$', lf.read_text(), re.M):
            roots.add(m.group(1).replace("\\\\", "\\").lower())
    rows, seen = [], set()
    for root in roots:
        for acf in glob.glob(str(Path(root) / "steamapps" / "appmanifest_*.acf")):
            text = Path(acf).read_text(encoding="utf-8", errors="replace")
            f = {}
            for key in ("appid", "name", "StateFlags", "SizeOnDisk", "LastPlayed"):
                m = re.search(f'"{key}"\\s+"([^"]*)"', text)
                if m:
                    f[key] = m.group(1)
            # ASCII-only, as the Dispatch verb: encoding-proof over ssh.
            name = ascii_only(f.get("name", ""))
            if f.get("appid") and name and f["appid"] not in seen:
                seen.add(f["appid"])
                rows.append({"appid": int(f["appid"]), "name": name,
                             "state": int(f.get("StateFlags", 0) or 0),
                             "size": int(f.get("SizeOnDisk", 0) or 0),
                             "lastPlayed": int(f.get("LastPlayed", 0) or 0)})
    return rows


# --- index file ---------------------------------------------------------------


def load():
    return cglib.load_json(LIBRARY, {})


def installed_name(appid):
    """Installed title for an appid, or None."""
    for r in load().get("installed", []):
        if r["appid"] == appid:
            return r["name"]
    return None


def save(index):
    _atomic_write(LIBRARY, index)               # tmp + os.replace


def refresh(local=False):
    try:
        rows = fetch_installed_local() if local else fetch_installed_ssh()
    except Exception as e:
        log.warn("sync_skipped", layer="installed", err=str(e))
        return 1
    index = load()
    index["refreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    index["installed"] = rows
    save(index)
    log("sync_done", layer="installed", games=len(rows))
    return 0


def fetch_collections_ssh():
    """Big Picture collections as [{name, id}]. Needs the PC awake."""
    import gamepc
    return parse_games_json(gamepc.collections())


def refresh_collections():
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


def show():
    index = load()
    rows = sorted(index.get("installed", []),
                  key=lambda r: r.get("lastPlayed", 0), reverse=True)
    if not rows:
        print("no index - run: python library.py refresh")
        return 1
    print(f"refreshed {index.get('refreshed', '?')} - {len(rows)} installed")
    for r in rows:
        last = (time.strftime("%Y-%m-%d", time.localtime(r["lastPlayed"]))
                if r.get("lastPlayed") else "never")
        print(f"  {r['appid']:>8}  {last}  {r['name']}")
    return 0


# --- layers 2-3: owned/playtime + metadata ------------------------------------

META_CACHE = STATE / "metadata-cache.json"
_CTRL = {28: "full", 18: "partial"}          # Steam category ids


def fetch_owned(api_key, steamid):
    """Account-global playtime + recency (ACF LastPlayed is per-machine).
    One call, own key only."""
    import requests
    r = requests.get(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
        params={"key": api_key, "steamid": steamid, "include_appinfo": 1,
                "include_played_free_games": 1, "format": "json"}, timeout=30)
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


def fetch_meta_one(appid):
    """appdetails (genres/controller/desc/score/year) + SteamSpy tags. Caller
    paces the requests."""
    import requests
    meta = {}
    r = requests.get("https://store.steampowered.com/api/appdetails",
                     params={"appids": appid}, timeout=20)
    d = r.json().get(str(appid), {})
    if d.get("success"):
        data = d["data"]
        cats = {c["id"] for c in data.get("categories", [])}
        meta.update({
            "genres": [g["description"] for g in data.get("genres", [])],
            "controller": next((v for k, v in _CTRL.items() if k in cats), "none"),
            "desc": re.sub(r"[^\x20-\x7E]", "",
                           data.get("short_description", ""))[:160],
            "score": (data.get("metacritic") or {}).get("score"),
            "year": (data.get("release_date") or {}).get("date", "")[-4:],
        })
    r2 = requests.get("https://steamspy.com/api.php",
                      params={"request": "appdetails", "appid": appid}, timeout=20)
    tags = r2.json().get("tags") or {}
    if isinstance(tags, dict):
        meta["tags"] = [t for t, _ in
                        sorted(tags.items(), key=lambda kv: -kv[1])[:10]]
    return meta


def load_meta():
    return cglib.load_json(META_CACHE, {})


def _save_meta(cache):
    _atomic_write(META_CACHE, cache)


def refresh_meta(appids, limit=200):
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
        time.sleep(2.1)
    return cache


def refresh_owned():
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
NOT_GAMES = {228980}                        # Steamworks Common Redistributables


def ascii_only(s):
    """ASCII-only: encoding-proof across every hop, (tm) glyphs are noise."""
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def fuzzy_key(name):
    """Letters and digits only: rogue-like == Roguelike, 'co op' == Co-op,
    and the facet-cache key."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# -- caches (every JSON write goes through _atomic_write: tmp + os.replace) ---

def _atomic_write(path, obj):
    cglib.write_json(path, obj, indent=1)


def steam_creds():
    """(steamApiKey, steamId64) when both are real, else None. Logs nothing:
    each caller decides whether a missing key is news."""
    s = cglib.load_secrets()
    key, sid = s.get("steamApiKey"), str(s.get("steamId64", ""))
    return (key, sid) if cglib.real_key(key) and sid.isdigit() else None


# --- the sync orchestrator (all layers, staleness- and key-gated) ------------
# Layer 1 needs the PC awake and fail-softs when asleep; layers 2-3 come from
# the Steam cloud, so the catalog stays current while the rig sleeps.
OWNED_MAX_AGE_S = 6 * 3600      # playtime/recency drift slowly; one call/6h
_sync_lock = threading.Lock()


def _iso_age(index, key):
    """Seconds since index[key] (iso timestamp), or None if absent/unparseable."""
    try:
        return time.time() - time.mktime(time.strptime(index[key],
                                                        "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        return None


def sync(meta_limit=200):
    """Full catalog refresh for the background thread: installed every call,
    owned when stale >6h, metadata top-up for new appids. Steam layers are
    skipped without keys. Non-reentrant, so calls can't stack meta crawls."""
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        # Layer 1b only when layer 1 SUCCEEDED - both need the PC awake, so
        # gating spares a sleeping sync (and the blind test) a 15 s ssh wait.
        if refresh() == 0:                          # layer 1 (fail-softs asleep)
            refresh_collections()                   # layer 1b (PC-dependent too)
        # Layer 4 is keyless, so it runs BEFORE the key gate. Reached through
        # the module attribute so the blind suite's patches bite.
        import steamstore
        d_age = _iso_age(steamstore.load_deals(), "refreshed")
        if d_age is None or d_age > steamstore.DEALS_MAX_AGE_S:
            steamstore.refresh_deals()
        if not steam_creds():
            return                                  # no Steam key: layers 1+4 only
        age = _iso_age(load(), "ownedRefreshed")
        if age is None or age > OWNED_MAX_AGE_S:
            refresh_owned()                         # layer 2
        index = load()
        appids = {r["appid"] for r in index.get("installed", [])}
        appids.update(int(a) for a in index.get("owned", {}))
        if any(str(a) not in load_meta() for a in appids):
            refresh_meta(list(appids), meta_limit)  # layer 3 (top-up only)
    except Exception as e:
        log.error("sync_failed", err=str(e))
    finally:
        _sync_lock.release()


def query_terms(limit=30):
    """Distinct tags/genres, frequency-ranked, fed to Flux as keyterms: titles
    alone don't teach the STT this vocabulary ("mech games" -> "met games")."""
    counts = {}
    for m in load_meta().values():
        for term in (m.get("tags") or []) + (m.get("genres") or []):
            t = term.lower()
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts, key=lambda t: -counts[t])[:limit]


def catalog_lines():
    """Compact rows for the assistant's context, installed first:
    appid|name|tags|genres|hours|lastPlayed|installed|ctrl"""
    index = load()
    meta = load_meta()
    owned = {k: v for k, v in index.get("owned", {}).items()
             if int(k) not in NOT_GAMES}
    installed_ids = {r["appid"] for r in index.get("installed", [])
                     if r["appid"] not in NOT_GAMES}
    rows = {r["appid"]: r["name"] for r in index.get("installed", [])
            if r["appid"] not in NOT_GAMES}
    for appid, o in owned.items():
        rows.setdefault(int(appid), o.get("name") or f"app {appid}")
    # Playtest/beta stubs have no metadata and pollute recommendations.
    rows = {a: n for a, n in rows.items() if not n.endswith(" Playtest")}
    lines = []
    for appid, name in rows.items():
        o = owned.get(str(appid), {})
        m = meta.get(str(appid), {})
        # Day precision: month-only rows made the model guess wrong about
        # what was played last (2026-08-15).
        last = (time.strftime("%Y-%m-%d", time.localtime(o["last"]))
                if o.get("last") else "never")
        lines.append((appid in installed_ids, o.get("hours", 0), (
            f"{appid}|{name}|{','.join(m.get('tags', [])[:5])}"
            f"|{','.join(m.get('genres', [])[:3])}|{o.get('hours', 0)}h"
            f"|{last}|{'inst' if appid in installed_ids else 'notinst'}"
            f"|{m.get('controller', '?')}")))
    lines.sort(key=lambda t: (not t[0], -t[1]))
    return [l for _, _, l in lines]


def catalog():
    lines = catalog_lines()
    text = "\n".join(lines)
    print(text)
    print(f"\n# {len(lines)} games, ~{len(text) // 4} tokens", file=sys.stderr)
    return 0


def usage():
    print("usage: library.py sync | refresh [--local-steam] [--owned] "
          "[--meta [N]] | show | catalog | probe <deals|search ...|reviews "
          "<appid>|news <appid>|hltb <name>|trending|recent>")
    return 2


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["sync"]:
        sync()
        sys.exit(0)
    elif args[:1] == ["refresh"]:
        rc = refresh(local="--local-steam" in args)
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
    elif args[:1] == ["probe"]:
        import steamstore
        sys.exit(steamstore.probe(args[1:]))
    else:
        sys.exit(usage())
