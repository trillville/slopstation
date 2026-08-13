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
    allpats = dispatch_patterns()
    assert len(allpats) == 7, f"expected 7 verbs, got {allpats}"
    for p in allpats:
        # \z, not $: '$' also matches before a trailing newline in .NET, and
        # this file is the whole remote attack surface. Applies to the
        # read-only verbs too - one lax anchor in here is one too many.
        assert p.startswith("^") and p.endswith(r"\z"), f"unanchored pattern: {p}"

    pats = [p for p in allpats if "--turn" in p]
    assert len(pats) == 3, f"expected enter/exit/launch to take a turn, got {pats}"
    for p in pats:
        assert "[0-9a-f]{1,8}" in p, f"pattern does not bound the turn: {p}"

    verbs = {"enter": "enter", "exit": "exit", "launch": "launch 12345"}
    for p in pats:
        verb = next(v for v in verbs if p.lstrip("^").startswith(v))
        base = verbs[verb]
        rx = compile_ps(p)
        # The happy path still works, with and without a turn.
        assert rx.match(base), f"{p} no longer matches the bare verb {base!r}"
        assert rx.match(f"{base} --turn 9f2c1a"), f"{p} rejects a good turn"
        # And every hostile string fails to match AT ALL, so Dispatch falls
        # through to DENIED rather than passing it on. Fail closed.
        for bad in HOSTILE:
            assert not rx.match(f"{base} --turn {bad}"), \
                f"{p} MATCHED hostile turn {bad!r} - path traversal reachable"
    print(f"  dispatch: {len(allpats)} verbs \\z-anchored, {len(pats)} hex-bounded, "
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

        # No turn at all is simply an untagged command.
        couch.ssh_intent("exit")
        assert sent[-1] == "exit", sent[-1]

        # -- THE TASK BOUNDARY -------------------------------------------------
        # This is the regression that shipped. A ContextVar is copied into a
        # task when that task is CREATED, so a turn minted inside a running
        # frame processor cannot reach the assistant's tool-dispatch task,
        # which is a sibling holding an older snapshot. Result on 2026-08-11:
        # every voice-driven exit arrived at the gaming PC with no turn, and
        # the launch ran under an id couch.py minted for itself instead of the
        # one the user's sentence created. Ambient absent, explicit present,
        # must still tag the wire - that asymmetry IS the bug.
        couch.ssh_intent("exit", turn="4c1d0e")
        assert sent[-1] == "exit --turn 4c1d0e", \
            f"explicit turn lost when ambient is empty: {sent[-1]!r}"

        # Explicit beats a stale ambient value rather than losing to it.
        tok = events.context(turn="9f2c1a")
        couch.ssh_intent("exit", turn="4c1d0e")
        assert sent[-1] == "exit --turn 4c1d0e", sent[-1]
        events.reset(tok)

        # A hostile explicit id is still dropped at the wire - the new
        # parameter must not become a way around the validation above.
        couch.ssh_intent("exit", turn="../../evil")
        assert sent[-1] == "exit", \
            f"explicit turn bypassed validation: {sent[-1]!r}"
    finally:
        couch.ssh = real_ssh
    print("  wire: valid turn tagged, malformed dropped, launch never blocked")
    print("  wire: explicit turn survives an empty/stale ambient, still validated")

    # -- Dispatch hands the id over without the ContextVar ----------------------
    # Same bug one layer up, asserted end to end: with NO ambient turn at all,
    # a Dispatch that was told the turn must still tag both machine-crossing
    # verbs. This is what GrammarGate does when it mints the id.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import cglib
    import dispatch as dp

    sent.clear()
    couch.ssh = lambda cmd, **kw: sent.append(cmd) or "OK"
    try:
        d = dp.Dispatch({"tvComPort": "COMX", "voice": {}}, cglib.CapturingLog("d"))
        assert d.turn is None, "a fresh Dispatch must not carry a stale id"
        d.turn = "4c1d0e"                       # what GrammarGate now writes
        d.end_session()
        assert sent[-1] == "exit --turn 4c1d0e", \
            f"voice-driven exit reached the PC uncorrelated: {sent[-1]!r}"
    finally:
        couch.ssh = real_ssh
    print("  dispatch: the exit verb carries the utterance's id, no ContextVar")

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
