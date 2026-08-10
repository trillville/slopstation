"""Game-library index builder (Project C).

Layer 1 (C2): installed games - `ssh gamepc games` (the Dispatch verb) when
run on the K15, or --local-steam (direct ACF scan) on the gaming PC itself
(dev/testing). Layers 2-3 (owned/playtime via Steam Web API, metadata via
appdetails + SteamSpy) arrive in C3 and merge into the same file.

Output: state/library.json  {"refreshed": iso-utc, "installed": [rows]}
written atomically (tmp + os.replace). Consumers: Flux keyterms, the grammar's
{game} slot, fuzzy launch resolution, and (C3) the assistant's catalog.

CLI:
    python library.py refresh [--local-steam] [--owned] [--meta [N]]
    python library.py show
    python library.py catalog
"""
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

import cglib

STATE = cglib.BASE / "state"
LIBRARY = STATE / "library.json"

log = cglib.make_log("library")


# --- layer 1 sources ----------------------------------------------------------

def fetch_installed_ssh():
    """The production path (K15): the gaming PC enumerates its own ACFs."""
    from couch import ssh
    return parse_games_json(ssh("games", timeout=30))


def parse_games_json(text):
    rows = json.loads(text.strip().lstrip("﻿"))
    if isinstance(rows, dict):          # single-game library edge
        rows = [rows]
    return rows


def fetch_installed_local():
    """Dev path (running ON the gaming PC): same fields, no ssh. Mirrors the
    Dispatch `games` verb so blind tests validate both against each other."""
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
            # ASCII-only names, same rule as the Dispatch verb (encoding-proof
            # across ssh/shell hops; (tm) glyphs are noise to voice anyway).
            name = re.sub(r"[^\x20-\x7E]", "", f.get("name", "")).strip()
            if f.get("appid") and name and f["appid"] not in seen:
                seen.add(f["appid"])
                rows.append({"appid": int(f["appid"]), "name": name,
                             "state": int(f.get("StateFlags", 0) or 0),
                             "size": int(f.get("SizeOnDisk", 0) or 0),
                             "lastPlayed": int(f.get("LastPlayed", 0) or 0)})
    return rows


# --- index file ---------------------------------------------------------------

def load():
    try:
        return json.loads(LIBRARY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(index):
    STATE.mkdir(exist_ok=True)
    tmp = LIBRARY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=1), encoding="utf-8")
    os.replace(tmp, LIBRARY)


def refresh(local=False):
    try:
        rows = fetch_installed_local() if local else fetch_installed_ssh()
    except Exception as e:
        log(f"library refresh skipped ({e})")
        return 1
    index = load()
    index["refreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    index["installed"] = rows
    save(index)
    log(f"library refreshed - {len(rows)} installed games")
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


# --- layers 2-3 (C3): owned/playtime + metadata -------------------------------

META_CACHE = STATE / "metadata-cache.json"
_CTRL = {28: "full", 18: "partial"}          # Steam category ids


def fetch_owned(api_key, steamid):
    """Steam Web API: account-global playtime + recency (the canonical
    source - ACF LastPlayed is per-machine). One call, own key only."""
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
            "name": re.sub(r"[^\x20-\x7E]", "", g.get("name", "")).strip(),
        }
    return out


def fetch_meta_one(appid):
    """appdetails (genres/controller/desc/score/year) + SteamSpy tags.
    Caller paces requests; results are cached forever."""
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
    try:
        return json.loads(META_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def refresh_meta(appids, limit=200):
    """Top up NEW appids only, ~1 req/2s (appdetails' unofficial ceiling)."""
    cache = load_meta()
    todo = [a for a in appids if str(a) not in cache][:limit]
    for i, appid in enumerate(todo):
        try:
            cache[str(appid)] = fetch_meta_one(appid)
            log(f"meta {appid} fetched ({i + 1}/{len(todo)})")
        except Exception as e:
            log(f"meta {appid} failed ({e}) - will retry next refresh")
        time.sleep(2.1)
    if todo:
        STATE.mkdir(exist_ok=True)
        tmp = META_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        os.replace(tmp, META_CACHE)
    return cache


def refresh_owned():
    sys.path.insert(0, str(cglib.BASE / "voice"))
    from voice_agent import load_secrets, real_key
    s = load_secrets()
    if not (real_key(s.get("steamApiKey")) and str(s.get("steamId64", "")).isdigit()):
        log("owned-layer skipped: steamApiKey/steamId64 not set in secrets.json")
        return 1
    index = load()
    index["owned"] = fetch_owned(s["steamApiKey"], s["steamId64"])
    index["ownedRefreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save(index)
    log(f"owned layer refreshed - {len(index['owned'])} games")
    return 0


def catalog_lines():
    """Compact rows for the assistant's context - one line per game,
    installed first. appid|name|tags|genres|hours|lastPlayed|installed|ctrl"""
    NOT_GAMES = {228980}                     # Steamworks Common Redistributables
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
    lines = []
    for appid, name in rows.items():
        o = owned.get(str(appid), {})
        m = meta.get(str(appid), {})
        last = (time.strftime("%Y-%m", time.localtime(o["last"]))
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
    print("usage: library.py refresh [--local-steam] [--owned] [--meta [N]] "
          "| show | catalog")
    return 2


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["refresh"]:
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
    else:
        sys.exit(usage())
