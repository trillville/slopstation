"""Blind test: library layer 1 + title resolution against the REAL Steam
install on this machine, cross-validated against the actual Dispatch.ps1
`games` verb (python scan and PowerShell verb must agree row-for-row), plus
the ARMORED-CORE-™-VI-vs-"armored core six" normalization problem. Run:
    .venv\\Scripts\\python tests\\test_library.py
"""
import _bootstrap  # noqa: F401
import subprocess
from pathlib import Path

import library
import titles
from grammar_gate import GrammarMatcher

REQUIRES = {"steam"}          # a local Steam install + Dispatch.ps1: the gaming PC

DISPATCH = Path(__file__).resolve().parents[3] / "gaming-pc" / "Dispatch.ps1"
VOICE_CFG = {"inputs": {"apple tv": "hdmi1"}}


def ps_games():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         f"$env:SSH_ORIGINAL_COMMAND='games'; & '{DISPATCH}'"],
        capture_output=True, text=True, timeout=60)
    assert r.stdout.strip(), f"games verb produced no output: {r.stderr[:300]}"
    return library.parse_games_json(r.stdout)


def main():
    assert library.refresh(local=True) == 0
    rows = library.load()["installed"]
    assert rows, "no installed games found"
    assert all({"appid", "name", "state", "size", "lastPlayed"} <= set(r) for r in rows)
    print(f"  local scan: {len(rows)} installed games")

    # Cross-validate the two independent implementations of layer 1.
    ps_rows = ps_games()
    ours = {(r["appid"], r["name"]) for r in rows}
    theirs = {(r["appid"], r["name"]) for r in ps_rows}
    assert ours == theirs, f"python vs PS verb drift: {ours ^ theirs}"
    print(f"  PS games verb agrees: {len(ps_rows)} rows identical")

    # Resolver: every installed game round-trips from its own spoken form...
    resolve = titles.build_resolver(87)
    for r in rows:
        appid, name = resolve(titles.spoken_form(r["name"]))
        assert appid == r["appid"], f"round-trip failed: {r['name']} -> {name}"
    # ...and garbage resolves to nothing.
    assert resolve("purple monkey dishwasher") == (None, None)
    # Bare pronouns/stopwords must NOT capture a title (token-subset scores
    # 100); "play it" has to fall through to the assistant lane.
    for stop in ("it", "the", "a", "on"):
        assert resolve(stop) == (None, None), f"'{stop}' wrongly resolved"
    print(f"  resolver: {len(rows)}/{len(rows)} round-trips, stopwords refused")

    # The realism cases that motivated titles.py (guarded on ownership).
    by_id = {r["appid"]: r["name"] for r in rows}
    if 1888160 in by_id:
        assert resolve("armored core six")[0] == 1888160
        assert resolve("armored core")[0] == 1888160
        print(f"  '{by_id[1888160]}' answers to 'armored core six' and 'armored core'")

    # Grammar + resolver combined: the full production path for "play X".
    m = GrammarMatcher(VOICE_CFG)
    hits = 0
    for r in rows[:10]:
        got = m.match(f"play {titles.spoken_form(r['name'])}")
        if (got and got[0] == "PlayGame"
                and resolve(str(got[1]["game"]))[0] == r["appid"]):
            hits += 1
    assert hits >= 8, f"grammar+resolver landed only {hits}/10 real titles"
    if 1888160 in by_id:
        got = m.match("play armored core six")
        assert got and got[0] == "PlayGame", got
        assert resolve(str(got[1]["game"]))[0] == 1888160
    print(f"  grammar+resolver: {hits}/10 real titles land from spoken phrasing")
    print("OK - library layer 1, PS cross-validation, resolver, grammar realism")


if __name__ == "__main__":
    main()
