"""Test title resolution against the local Steam installation."""

import subprocess

import helpers
from slopstation.agent.speech.grammar_gate import GrammarMatcher
from slopstation.agent.tools import library, titles

DISPATCH = helpers.REPO / "gaming-pc" / "Dispatch.ps1"
VOICE_CFG = {"inputs": {"apple tv": "hdmi1"}}


def ps_games():
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"$env:SSH_ORIGINAL_COMMAND='games'; & '{DISPATCH}'",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.stdout.strip(), f"games verb produced no output: {r.stderr[:300]}"
    return library.parse_games_json(r.stdout)


def test_library():
    helpers.wants("steam")
    rows = ps_games()
    assert rows, "no installed games found"
    keys = {"appid", "name", "state", "size", "lastPlayed", "updated"}
    assert all(keys <= set(r) for r in rows)
    # The index lands under this test's runtime home (conftest).
    library.save({"installed": rows})

    # Resolver: every installed game round-trips from its own spoken form...
    resolve = titles.build_resolver(87)
    for r in rows:
        appid, name = resolve(titles.spoken_form(r["name"]))
        assert appid == r["appid"], f"round-trip failed: {r['name']} -> {name}"
    # ...and garbage resolves to nothing.
    assert resolve("purple monkey dishwasher") == (None, None)
    # Stopwords must not capture a title - token-subset scores them 100.
    for stop in ("it", "the", "a", "on"):
        assert resolve(stop) == (None, None), f"'{stop}' wrongly resolved"

    # Normalization realism, guarded on ownership.
    by_id = {r["appid"]: r["name"] for r in rows}
    if 1888160 in by_id:
        assert resolve("armored core six")[0] == 1888160
        assert resolve("armored core")[0] == 1888160

    # Exercise grammar and title resolution together.
    m = GrammarMatcher(VOICE_CFG)
    hits = 0
    for r in rows[:10]:
        got = m.match(f"play {titles.spoken_form(r['name'])}")
        if (
            got
            and got[0] == "PlayGame"
            and resolve(str(got[1]["game"]))[0] == r["appid"]
        ):
            hits += 1
    assert hits >= 8, f"grammar+resolver landed only {hits}/10 real titles"
    if 1888160 in by_id:
        got = m.match("play armored core six")
        assert got and got[0] == "PlayGame", got
        assert resolve(str(got[1]["game"]))[0] == 1888160
