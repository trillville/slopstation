"""Test turn-ID validation and propagation to gaming-PC tasks."""

import re

import pytest

import helpers
from helpers import CapturingLog
from slopstation import couch, events, gamepc
from slopstation.agent.brain import dispatch as dp

DISPATCH = helpers.REPO / "gaming-pc" / "Dispatch.ps1"

# Strings that must never reach a filename on the gaming PC.
HOSTILE = [
    "../../evil",
    "..\\..\\evil",
    "a/b",
    "a\\b",
    "C:\\x",
    "$(whoami)",
    "`whoami`",
    "a b",
    "a;b",
    "a&b",
    "a|b",
    "a>b",
    "*",
    "?",
    "..",
    ".",
    "con",
    "aux",
    "-",
    "--",
    "%TEMP%",
    "9f2c1a ",
    " 9f2c1a",
    "9F2C1A",
    "9f2c1a1234",
    "",
    "zzzz",
    "9f2c-1a",
    "9f2c1a\n",
    "9f2c1a\r\nenter",
]


def dispatch_patterns():
    """Every verb pattern from the switch -Regex in Dispatch.ps1, read from the
    file so this can never test a stale copy."""
    text = DISPATCH.read_text(encoding="utf-8")
    return re.findall(r"^\s*'(\^[^']+)'", text, re.MULTILINE)


def compile_ps(pattern):
    """.NET spells absolute-end-of-string \\z; Python spells it \\Z and has no
    \\z. Translate rather than weaken the shipping pattern."""
    return re.compile(pattern.replace(r"\z", r"\Z"))


@pytest.fixture
def wire(monkeypatch):
    """gamepc.ssh replaced: every command lands on the list, the PC says OK."""
    sent = []
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: sent.append(cmd) or "OK")
    return sent


def test_minting_is_collision_free_and_self_valid():
    ids = {events.new_turn() for _ in range(2000)}
    assert len(ids) > 1990, f"only {len(ids)} unique in 2000 - collision-prone"
    assert all(events.valid_turn(i) for i in ids), "minted an id we would reject"
    assert all(re.fullmatch(r"[0-9a-f]{6}", i) for i in ids)


def test_validation_rejects_every_hostile_shape():
    for bad in HOSTILE:
        assert not events.valid_turn(bad), f"valid_turn accepted {bad!r}"
    assert not events.valid_turn(None) and not events.valid_turn(123)
    for good in ("0", "9f2c1a", "abcdef12"):
        assert events.valid_turn(good), good


def test_dispatch_ps1_is_the_real_boundary():
    # 15 patterns across 13 verbs: nav alone is three (front-page/library,
    # game-page with an appid, collection), so this counts patterns, not verbs.
    allpats = dispatch_patterns()
    assert len(allpats) == 15, f"expected 15 patterns, got {len(allpats)}: {allpats}"
    for p in allpats:
        # \z, not $: in .NET '$' also matches before a trailing newline.
        assert p.startswith("^") and p.endswith(r"\z"), f"unanchored pattern: {p}"

    # gamepc.py mirrors the switch: one function per verb, the same set.
    verbs = {re.match(r"\^(\w+)", p).group(1) for p in allpats}
    assert verbs == set(gamepc.VERBS), (
        f"verbs: Dispatch {sorted(verbs)} vs gamepc {sorted(gamepc.VERBS)}"
    )
    assert all(callable(getattr(gamepc, v)) for v in gamepc.VERBS)

    pats = [p for p in allpats if "--turn" in p]
    # Six mutating verbs take a turn, and nav is three patterns, so eight
    # patterns carry one: enter, exit, rescan, launch, nav x3, stop.
    assert len(pats) == 8, f"expected 8 turn-bearing patterns, got {pats}"
    for p in pats:
        assert "[0-9a-f]{1,8}" in p, f"pattern does not bound the turn: {p}"

    # One bare example per turn-bearing form (a list, since nav's three
    # patterns share the prefix). The collection ids are real shapes off the
    # rig: a tighter charset DENIES some of them.
    bases = [
        "enter",
        "exit",
        "rescan",
        "launch 12345",
        "nav downloads",
        "nav details 12345",
        "nav store 12345",
        "nav collection favorite",
        "nav collection uc-mkD+r+pfQ1hu",
        "nav collection uc-odwxN*+G1zDb*+",
        "stop 12345",
    ]
    for p in pats:
        rx = compile_ps(p)
        matched = [b for b in bases if rx.match(b)]
        assert matched, f"{p} matches none of the example bases"
        for base in matched:
            assert rx.match(f"{base} --turn 9f2c1a"), (
                f"{p} rejects a good turn on {base!r}"
            )
            # No match at all, so Dispatch falls through to DENIED.
            for bad in HOSTILE:
                assert not rx.match(f"{base} --turn {bad}"), (
                    f"{p} MATCHED hostile turn {bad!r} on {base!r} - path traversal reachable"
                )


def test_the_wire_carries_a_valid_turn_and_refuses_an_uncorrelated_beat(wire):
    tok = events.context(turn="9f2c1a")
    gamepc.ssh_intent("enter")
    assert wire[-1] == "enter --turn 9f2c1a", wire[-1]
    events.reset(tok)

    # A malformed id must not go on the wire: Dispatch fails closed, so a
    # telemetry bug would become a launch outage.
    tok = events.context(turn="../../evil")
    gamepc.ssh_intent("enter")
    assert wire[-1] == "enter", f"malformed turn reached the wire: {wire[-1]!r}"
    events.reset(tok)

    gamepc.ssh_intent("exit")
    assert wire[-1] == "exit", wire[-1]

    # -- the task boundary -------------------------------------------------
    # A ContextVar is copied into a task when that task is CREATED, so a
    # turn minted in a running frame processor cannot reach the assistant's
    # tool-dispatch task. Explicit-with-no-ambient must still tag the wire.
    gamepc.ssh_intent("exit", turn="4c1d0e")
    assert wire[-1] == "exit --turn 4c1d0e", (
        f"explicit turn lost when ambient is empty: {wire[-1]!r}"
    )

    # Explicit beats a stale ambient value rather than losing to it.
    tok = events.context(turn="9f2c1a")
    gamepc.ssh_intent("exit", turn="4c1d0e")
    assert wire[-1] == "exit --turn 4c1d0e", wire[-1]
    events.reset(tok)

    # A hostile explicit id is still dropped - the parameter is no bypass.
    gamepc.ssh_intent("exit", turn="../../evil")
    assert wire[-1] == "exit", f"explicit turn bypassed validation: {wire[-1]!r}"


def test_dispatch_hands_the_id_over_without_the_contextvar(wire, monkeypatch):
    # With no ambient turn, a Dispatch that was told the turn must still tag
    # both machine-crossing verbs - what GrammarGate does when it mints the id.
    d = dp.Dispatch({"tvComPort": "COMX", "voice": {}}, CapturingLog("d"))
    assert d.utterance == dp.Utterance(None, None), (
        "a fresh Dispatch must not carry a stale utterance"
    )
    d.begin_utterance("4c1d0e", "end the session")  # the gate's one write
    d.end_session()
    assert wire[-1] == "exit --turn 4c1d0e", (
        f"voice-driven exit reached the PC uncorrelated: {wire[-1]!r}"
    )

    # A second utterance lands while the first's dispatch is on the wire;
    # consumers snapshot at operation start, so the re-point can't reach it.
    def ssh_mid_flight(cmd, **kw):
        wire.append(cmd)
        d.begin_utterance("bbbbbb", "never mind")  # barge-in mid-ssh
        return "OK"

    d.begin_utterance("aaaaaa", "end the session")
    monkeypatch.setattr(gamepc, "ssh", ssh_mid_flight)
    d.end_session()
    assert wire[-1] == "exit --turn aaaaaa", (
        f"a mid-flight utterance re-labeled an in-flight action: {wire[-1]!r}"
    )


def test_couch_cli_takes_the_turn_off_argv():
    argv = ["start", "12345", "--turn", "9f2c1a"]
    assert couch.take_turn(argv) == "9f2c1a" and argv == ["start", "12345"]
    argv = ["start", "--turn", "9f2c1a", "12345"]
    assert couch.take_turn(argv) == "9f2c1a" and argv == ["start", "12345"]
    argv = ["start"]
    assert couch.take_turn(argv) is None and argv == ["start"]
    # A trailing --turn with no value must not IndexError the launch away.
    argv = ["start", "--turn"]
    assert couch.take_turn(argv) is None and argv == ["start"]
