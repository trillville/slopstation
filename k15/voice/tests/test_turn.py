"""Blind test: the turn id - one id per user intent, carried from the wake
word or the chord all the way to the gaming PC's scheduled task.

The security case is the reason this file exists. On the far side the id
reaches a FILENAME, so Dispatch.ps1's patterns are the boundary that stops a
path-traversal string ever getting there. Those patterns are read OUT OF THE
SHIPPING SCRIPT here rather than copied, so the test cannot drift away from
what actually runs. Run:
    .venv\\Scripts\\python tests\\test_turn.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import couch
import events

DISPATCH = Path(__file__).resolve().parents[3] / "gaming-pc" / "Dispatch.ps1"

# Strings that must never reach a filename on the gaming PC.
HOSTILE = [
    "../../evil", "..\\..\\evil", "a/b", "a\\b", "C:\\x", "$(whoami)",
    "`whoami`", "a b", "a;b", "a&b", "a|b", "a>b", "*", "?", "..", ".",
    "con", "aux", "-", "--", "%TEMP%", "9f2c1a ", " 9f2c1a", "9F2C1A",
    "9f2c1a1234", "", "zzzz", "9f2c-1a", "9f2c1a\n", "9f2c1a\r\nenter",
]


def dispatch_patterns():
    """Every verb pattern from the switch -Regex in Dispatch.ps1, read from
    the file so this can never test a stale copy."""
    text = DISPATCH.read_text(encoding="utf-8")
    return re.findall(r"^\s*'(\^[^']+)'", text, re.MULTILINE)


def compile_ps(pattern):
    """.NET spells absolute-end-of-string \\z; Python spells it \\Z (and has no
    \\z at all). Same meaning, different dialect - translate rather than weaken
    the shipping pattern to something both engines happen to accept."""
    return re.compile(pattern.replace(r"\z", r"\Z"))


def main():

    # -- minting ---------------------------------------------------------------
    ids = {events.new_turn() for _ in range(2000)}
    assert len(ids) > 1990, f"only {len(ids)} unique in 2000 - collision-prone"
    assert all(events.valid_turn(i) for i in ids), "minted an id we would reject"
    assert all(re.fullmatch(r"[0-9a-f]{6}", i) for i in ids)
    print(f"  mint: {len(ids)} unique 6-hex ids, all self-validating")

    # -- validation ------------------------------------------------------------
    for bad in HOSTILE:
        assert not events.valid_turn(bad), f"valid_turn accepted {bad!r}"
    assert not events.valid_turn(None) and not events.valid_turn(123)
    for good in ("0", "9f2c1a", "abcdef12"):
        assert events.valid_turn(good), good
    print(f"  validate: rejected all {len(HOSTILE)} hostile strings")

    # -- Dispatch.ps1 is the real boundary -------------------------------------
    # 12 PATTERNS across 10 verbs: nav alone is three (a front-page/library
    # form, a game-page form with an appid, and a collection form), which is
    # why this counts patterns, not verbs.
    allpats = dispatch_patterns()
    assert len(allpats) == 12, f"expected 12 patterns, got {len(allpats)}: {allpats}"
    for p in allpats:
        # \z, not $: in .NET '$' also matches before a trailing newline, and
        # this file is the whole remote attack surface - read-only verbs too.
        assert p.startswith("^") and p.endswith(r"\z"), f"unanchored pattern: {p}"

    pats = [p for p in allpats if "--turn" in p]
    # The FIVE mutating verbs take a turn - and nav is three of the patterns,
    # so seven patterns carry one: enter, exit, launch, nav x3, stop.
    assert len(pats) == 7, f"expected 7 turn-bearing patterns, got {pats}"
    for p in pats:
        assert "[0-9a-f]{1,8}" in p, f"pattern does not bound the turn: {p}"

    # One bare example per turn-bearing form; each pattern must match at least
    # one, and each match must accept a good turn and reject EVERY hostile one.
    # A list (not a name->example dict) because nav's three patterns share the
    # 'nav' prefix and each needs its own example.
    # The collection examples are REAL id shapes off the rig - Steam's are
    # base64-ish, and a tighter reading of that charset silently DENIED 3 of
    # this rig's 11 collections (2026-08-14). Drill what the PC emits, not a
    # tidy invention.
    bases = ["enter", "exit", "launch 12345", "nav downloads",
             "nav details 12345", "nav store 12345", "nav collection favorite",
             "nav collection uc-mkD+r+pfQ1hu", "nav collection uc-odwxN*+G1zDb*+",
             "stop 12345"]
    for p in pats:
        rx = compile_ps(p)
        matched = [b for b in bases if rx.match(b)]
        assert matched, f"{p} matches none of the example bases"
        for base in matched:
            assert rx.match(f"{base} --turn 9f2c1a"), f"{p} rejects a good turn on {base!r}"
            # Every hostile string fails to match AT ALL, so Dispatch falls
            # through to DENIED rather than passing it on. Fail closed.
            for bad in HOSTILE:
                assert not rx.match(f"{base} --turn {bad}"), \
                    f"{p} MATCHED hostile turn {bad!r} on {base!r} - path traversal reachable"
    print(f"  dispatch: {len(allpats)} patterns \\z-anchored, {len(pats)} hex-bounded, "
          f"{len(HOSTILE)} hostile turns all DENIED")

    # -- the wire: uncorrelated beats refused ----------------------------------
    sent = []
    real_ssh = couch.ssh
    couch.ssh = lambda cmd, **kw: sent.append(cmd) or "OK"
    try:
        tok = events.context(turn="9f2c1a")
        couch.ssh_intent("enter")
        assert sent[-1] == "enter --turn 9f2c1a", sent[-1]
        events.reset(tok)

        # A malformed id must not go on the wire: Dispatch fails CLOSED, so
        # shipping one would turn a telemetry bug into a launch outage.
        tok = events.context(turn="../../evil")
        couch.ssh_intent("enter")
        assert sent[-1] == "enter", f"malformed turn reached the wire: {sent[-1]!r}"
        events.reset(tok)

        couch.ssh_intent("exit")
        assert sent[-1] == "exit", sent[-1]

        # -- THE TASK BOUNDARY -------------------------------------------------
        # A ContextVar is copied into a task when that task is CREATED, so a
        # turn minted inside a running frame processor cannot reach the
        # assistant's tool-dispatch task - a sibling holding an older snapshot.
        # Ambient absent + explicit present must still tag the wire.
        couch.ssh_intent("exit", turn="4c1d0e")
        assert sent[-1] == "exit --turn 4c1d0e", \
            f"explicit turn lost when ambient is empty: {sent[-1]!r}"

        # Explicit beats a stale ambient value rather than losing to it.
        tok = events.context(turn="9f2c1a")
        couch.ssh_intent("exit", turn="4c1d0e")
        assert sent[-1] == "exit --turn 4c1d0e", sent[-1]
        events.reset(tok)

        # A hostile explicit id is still dropped - the parameter is no bypass.
        couch.ssh_intent("exit", turn="../../evil")
        assert sent[-1] == "exit", \
            f"explicit turn bypassed validation: {sent[-1]!r}"
    finally:
        couch.ssh = real_ssh
    print("  wire: valid turn tagged, malformed dropped, launch never blocked")
    print("  wire: explicit turn survives an empty/stale ambient, still validated")

    # -- Dispatch hands the id over without the ContextVar ----------------------
    # The same shape one layer up: with NO ambient turn, a Dispatch that was
    # told the turn must still tag both machine-crossing verbs - which is what
    # GrammarGate does when it mints the id.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import cglib
    import dispatch as dp

    sent.clear()
    couch.ssh = lambda cmd, **kw: sent.append(cmd) or "OK"
    try:
        d = dp.Dispatch({"tvComPort": "COMX", "voice": {}}, cglib.CapturingLog("d"))
        assert d.utterance == dp.Utterance(None, None), \
            "a fresh Dispatch must not carry a stale utterance"
        d.begin_utterance("4c1d0e", "end the session")  # the gate's one write
        d.end_session()
        assert sent[-1] == "exit --turn 4c1d0e", \
            f"voice-driven exit reached the PC uncorrelated: {sent[-1]!r}"

        # The interleave the snapshot contract exists for: a SECOND utterance
        # lands while the first's dispatch is still on the wire. Consumers
        # snapshot at operation start, so the re-point cannot reach them.
        def ssh_mid_flight(cmd, **kw):
            sent.append(cmd)
            d.begin_utterance("bbbbbb", "never mind")   # barge-in mid-ssh
            return "OK"
        d.begin_utterance("aaaaaa", "end the session")
        couch.ssh = ssh_mid_flight
        d.end_session()
        assert sent[-1] == "exit --turn aaaaaa", \
            f"a mid-flight utterance re-labeled an in-flight action: {sent[-1]!r}"
    finally:
        couch.ssh = real_ssh
    print("  dispatch: the exit verb carries the utterance's id, no ContextVar")
    print("  dispatch: an in-flight action keeps its snapshot through a barge-in")

    # -- couch.py CLI ----------------------------------------------------------
    argv = ["start", "12345", "--turn", "9f2c1a"]
    assert couch.take_turn(argv) == "9f2c1a" and argv == ["start", "12345"]
    argv = ["start", "--turn", "9f2c1a", "12345"]
    assert couch.take_turn(argv) == "9f2c1a" and argv == ["start", "12345"]
    argv = ["start"]
    assert couch.take_turn(argv) is None and argv == ["start"]
    # A trailing --turn with no value must not IndexError the launch away.
    argv = ["start", "--turn"]
    assert couch.take_turn(argv) is None and argv == ["start"]
    print("  cli: --turn parsed in any position, and a bare --turn is harmless")

    print("OK - turn: minting, validation, the Dispatch boundary, wire, CLI")


if __name__ == "__main__":
    main()
