"""Game-library index builder (Project C).

Layer 1 (C2): installed games - `ssh gamepc games` (the Dispatch verb) when
run on the K15, or --local-steam (direct ACF scan) on the gaming PC itself
(dev/testing). Layers 2-3 (owned/playtime via Steam Web API, metadata via
appdetails + SteamSpy) arrive in C3 and merge into the same file.

Output: state/library.json  {"refreshed": iso-utc, "installed": [rows]}
written atomically (tmp + os.replace). Consumers: Flux keyterms, the grammar's
{game} slot, fuzzy launch resolution, and (C3) the assistant's catalog.

CLI:
    python library.py refresh [--local-steam]
    python library.py show
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


def usage():
    print("usage: library.py refresh [--local-steam] | show")
    return 2


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["refresh"]:
        sys.exit(refresh(local="--local-steam" in args))
    elif args[:1] == ["show"]:
        sys.exit(show())
    else:
        sys.exit(usage())
