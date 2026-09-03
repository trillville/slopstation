"""Dispatch.py logic with every side effect mocked - lock arbiter,
dry-run, volume stepping + clamp, mute, input map + the READY-gate on the
gaming input, serial retry, ssh outcomes.
"""

import subprocess
import time

import pytest

from helpers import CapturingLog, seed_lock
from slopstation import gamepc, sessionlock, tv  # gamepc: the ssh seam
from slopstation.agent.brain import dispatch as dp
from slopstation.agent.tools import library

CFG = {
    "tvComPort": "COMX",
    "tvGamingCmd": "hdmi4",
    "voice": {
        "volumeStep": 3,
        "volumeMax": 40,
        "inputs": {"apple tv": "hdmi1", "the pc": "hdmi4"},
    },
}


def harness(dry_run=False, on_end_session=None):
    """A Dispatch over CFG and a capturing log, as a lane builds one:
    (dispatch, log)."""
    log = CapturingLog("dispatch")
    return dp.Dispatch(CFG, log, dry_run=dry_run, on_end_session=on_end_session), log


def ssh_down(cmd, **kw):
    raise RuntimeError("unreachable")


@pytest.fixture
def sent(monkeypatch):
    """Every Ex-Link frame Dispatch sends, in order; the TV acks each one."""
    frames = []
    monkeypatch.setattr(
        tv, "exlink_send_hex", lambda frame, port: frames.append(frame) or tv.EXLINK_ACK
    )
    return frames


@pytest.fixture
def spawned(monkeypatch):
    """Every argv a cold start hands to Popen, instead of a couch console."""
    argvs = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: argvs.append(args))
    return argvs


@pytest.fixture
def host(monkeypatch):
    """The gaming PC's answer: host("OK") makes every ssh verb answer OK,
    host(fn) installs fn as gamepc.ssh."""

    def _host(reply):
        if callable(reply):
            monkeypatch.setattr(gamepc, "ssh", reply)
        else:
            monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: reply)

    return _host


def test_lock_arbiter():
    seed_lock(10)  # fresh lock
    d, _ = harness(dry_run=True)
    r = d.start_session()
    assert not r.ok and r.earcon == "busy", r

    seed_lock(None)
    d, log = harness(dry_run=True)
    r = d.start_session(appid=12345)
    assert r.ok and "couch.py start 12345" in r.detail, r
    # Assert on the EVENT, not its prose: dashboards group by event name.
    assert "dry_run_would" in log.events(), log.records

    seed_lock(999)  # stale lock = launchable
    d, _ = harness(dry_run=True)
    assert d.start_session().ok


def test_volume_steps_clamps_and_mutes(monkeypatch, sent):
    monkeypatch.setattr(time, "sleep", lambda s: None)  # the inter-step pause
    d, _ = harness()
    assert d.volume_up().ok
    assert sent == [tv.EXLINK_FRAMES["vol_up"]] * 3, sent  # volumeStep=3

    sent.clear()
    r = d.volume_set(80)  # clamps to volumeMax 40
    assert r.ok and sent == [tv.vol_set_frame(40)], sent
    sent.clear()
    assert d.volume_set(25).ok and sent == [tv.vol_set_frame(25)]

    sent.clear()
    assert d.mute_toggle().ok and sent == [tv.EXLINK_FRAMES["mute_toggle"]]


def test_a_serial_send_that_raises_is_a_fail_earcon(monkeypatch):
    # The COM retry lives in tv.exlink_send_hex, so here a raise is final.
    def always_down(frame, port):
        raise OSError("dead")

    monkeypatch.setattr(tv, "exlink_send_hex", always_down)
    d, _ = harness()
    r = d.mute_toggle()
    assert not r.ok and r.earcon == "fail"


def test_input_map_and_gaming_input_semantics(sent, spawned, host):
    d, _ = harness()
    assert not d.switch_input("garage").ok  # unknown name
    assert d.switch_input("Apple TV ").ok
    assert sent == [tv.EXLINK_FRAMES["hdmi1"]]

    # No session: "switch to the pc" means "start a session" - spawns the full
    # couch launch and never touches the TV (couch.py flips at READY).
    seed_lock(None)
    sent.clear()
    r = d.switch_input("the pc")
    assert r.ok and spawned and spawned[0][-1] == "start" and not sent, (
        r,
        spawned,
        sent,
    )
    # Mid-launch (fresh lock, host pre-READY): truthful busy, no switch.
    seed_lock(10)
    host("NOTREADY")
    sent.clear()
    r = d.switch_input("the pc")
    assert not r.ok and r.earcon == "busy" and not sent, r
    host("2026-08-10T20:00:00")  # READY timestamp
    assert d.switch_input("the pc").ok and sent == [tv.EXLINK_FRAMES["hdmi4"]]

    # Fresh lock but host unreachable: honest fail, no switch.
    host(ssh_down)
    sent.clear()
    assert not d.switch_input("the pc").ok and not sent


def test_end_session_over_ssh_outcomes(host):
    # With the rig busy, ending writes the cancel marker couch.py consumes
    # BEFORE the ssh, so the launch stands down even if the exit never lands.
    seed_lock(10)
    host("OK")
    d, log = harness()
    assert d.end_session().ok
    assert sessionlock.cancel_file().exists(), "a busy rig's end must leave the marker"
    assert "end_session_dispatched" in log.events()
    seed_lock(10)
    host("FAILED:1")
    d, _ = harness()
    assert not d.end_session().ok
    seed_lock(10)  # mid-launch, PC mid-wake: still an end
    host(ssh_down)
    d, log = harness()
    r = d.end_session()
    assert r.ok and sessionlock.cancel_file().exists(), r
    assert "end_session_dispatched" in log.events()


def test_end_session_on_an_idle_rig_is_a_failure(host):
    # Nothing to cancel, so an unreachable PC is a real failure, and no marker.
    host(ssh_down)
    d, _ = harness()
    r = d.end_session()
    assert not r.ok and r.earcon == "fail"
    assert not sessionlock.cancel_file().exists()


def test_end_session_restores_the_room_before_the_exit(host):
    # The room ducker restores HERE, before the exit: the voice session stays
    # open for the idle timeout, by which time couch has cut TV power and
    # remote keys relay nothing.
    seed_lock(10)
    order = []
    host(lambda cmd, **kw: order.append(f"ssh {cmd}") or "OK")
    d, _ = harness(on_end_session=lambda: order.append("restore"))
    assert d.end_session().ok
    assert order[0] == "restore", order
    # A hook that raises is a warn, never a failed teardown.
    d, log = harness(on_end_session=lambda: 1 / 0)
    assert d.end_session().ok
    assert "end_session_hook_failed" in log.events(), log.records
    # Dry run restores too - the TV is real even when the ssh is not.
    hooked = []
    d, _ = harness(dry_run=True, on_end_session=lambda: hooked.append(1))
    assert d.end_session().ok and hooked == [1]


def test_play_game_session_live_ssh_outcomes_and_cold_start(monkeypatch, host, spawned):
    seed_lock(10)  # fresh lock = session up
    d, _ = harness()
    host("OK")
    assert d.play_game(1888160).ok
    host("ALREADY")
    assert d.play_game(1).ok
    host("BUSY:42")
    r = d.play_game(1)
    # The blocker is named for the assistant lane; an index miss degrades to
    # the bare id, never to a crash.
    assert not r.ok and r.earcon == "busy" and "BUSY:42" in r.detail, r
    monkeypatch.setattr(
        library, "installed_name", lambda a: {42: "Baldur's Gate 3"}.get(a)
    )
    r = d.play_game(1)
    assert "Baldur's Gate 3 is already running" in r.detail, r
    assert "quit" in r.detail, r  # the BUSY message now OFFERS the quit
    monkeypatch.setattr(library, "installed_name", lambda a: None)
    assert "app 42 is already running" in d.play_game(1).detail
    host("NOTREADY")  # launch still in flight
    r = d.play_game(1)
    assert not r.ok and r.earcon == "busy"
    host("NOTINSTALLED")  # PC-side install guard
    r = d.play_game(1)
    assert not r.ok and r.earcon == "fail" and "not installed" in r.detail
    assert "controller" in r.detail, r
    host(ssh_down)
    d, _ = harness()
    assert d.play_game(1).earcon == "fail"
    seed_lock(None)  # cold: full couch launch
    d, _ = harness()
    r = d.play_game(777)
    # Positional, not tail-anchored: a turn id may follow the appid.
    i = spawned[0].index("start")
    assert r.ok and spawned[0][i : i + 2] == ["start", "777"], spawned


def test_quit_game_correlated_wire_ssh_outcomes_and_wrong_game_refusal(
    monkeypatch, host
):
    monkeypatch.setattr(library, "installed_name", lambda a: None)
    d, log = harness()
    wire = []
    host(lambda cmd, **kw: wire.append(cmd) or "OK")
    d.begin_utterance("9f2c1a", "quit the game")
    r = d.quit_game(1888160)
    assert r.ok and "quitting" in r.detail, r
    # The appid rides the verb and the turn tags it: a stop is mutating.
    assert wire[-1] == "stop 1888160 --turn 9f2c1a", wire[-1]
    assert "quit_dispatched" in log.events()
    host("NOTRUNNING")
    r = d.quit_game(1)
    assert r.ok and "nothing is running" in r.detail, r
    host("BUSY:42")  # a DIFFERENT game is up
    monkeypatch.setattr(
        library, "installed_name", lambda a: {42: "Baldur's Gate 3"}.get(a)
    )
    r = d.quit_game(1)
    assert not r.ok and r.earcon == "busy" and "Baldur's Gate 3" in r.detail, r
    monkeypatch.setattr(library, "installed_name", lambda a: None)
    host(ssh_down)
    d, _ = harness()
    assert d.quit_game(1).earcon == "fail"


def test_nav_correlated_wire_per_kind_notready_and_unknown_kind_refusal(
    monkeypatch, host
):
    monkeypatch.setattr(library, "installed_name", lambda a: None)
    seed_lock(None)
    d, log = harness()
    wire = []
    host(lambda cmd, **kw: wire.append(cmd) or "OK")
    d.begin_utterance("4c1d0e", "show downloads")
    assert d.nav("downloads").ok and wire[-1] == "nav downloads --turn 4c1d0e", wire
    assert "nav_dispatched" in log.events()
    assert d.nav("details", 400).ok and wire[-1] == "nav details 400 --turn 4c1d0e", (
        wire
    )
    assert d.nav("store", 400).ok and wire[-1] == "nav store 400 --turn 4c1d0e", wire
    assert (
        d.nav("collection", "uc-abc").ok
        and wire[-1] == "nav collection uc-abc --turn 4c1d0e"
    ), wire
    host("NOTREADY")
    r = d.nav("downloads")
    assert not r.ok and r.earcon == "busy", r
    assert "start one first" in r.detail, r
    # Mid-start (fresh lock) is a distinct busy from no-session: the reply must
    # not tell the model to start what is already starting.
    seed_lock(10)
    r = d.nav("downloads")
    assert not r.ok and r.earcon == "busy" and "starting" in r.detail, r
    seed_lock(None)
    # An unknown kind is refused HERE and never reaches the wire.
    wire2 = []
    host(lambda cmd, **kw: wire2.append(cmd) or "OK")
    r = d.nav("bogus")
    assert not r.ok and r.earcon == "fail" and not wire2, (r, wire2)
    host(ssh_down)
    d, _ = harness()
    assert d.nav("downloads").earcon == "fail"


def test_an_unregistered_task_says_so_on_every_verb_that_fires_one(monkeypatch, host):
    # The reply must name the task and the Register-ScheduledTask fix, not read
    # as a broken verb.
    d, _ = harness()
    monkeypatch.setattr(library, "installed_name", lambda a: None)
    seed_lock(5)  # a live session, so play_game takes the ssh path
    for verb, task, call in (
        ("nav", "Nav", lambda: d.nav("downloads")),
        ("stop", "StopGame", lambda: d.quit_game(1)),
        ("launch", "LaunchGame", lambda: d.play_game(1)),
    ):
        host(lambda cmd, _t=task, **kw: f"NOTASK:{_t}")
        r = call()
        assert not r.ok and task in r.detail and "registered" in r.detail, (verb, r)
