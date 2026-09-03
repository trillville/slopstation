"""Couch.py's orchestration state machine, with every side effect
stubbed at its seam (ssh, exlink, wol, wait_port).
"""

import os
import threading
import time

import pytest

from helpers import CapturingLog, seed_lock
from slopstation import config, couch, gamepc, sessionlock, tv

CFG = {
    "tvComPort": "COMX",
    "tvGamingCmd": "hdmi4",
    "tvIdleCmd": "hdmi1",
    "tvOffWhenDone": True,
    "sshHost": "gamepc",
    "gamingPcIp": "127.0.0.1",
    "gamingPcMac": "00-00-00-00-00-00",
}

READY_TS = "2026-08-12T20:00:00"  # what a legacy marker answers


@pytest.fixture
def rig(monkeypatch):
    """The bench every launch runs on: CFG in place of config.json, no real
    sleeping, and the windows shrunk so a timed-out wait ends in well under a
    second. A case that needs a longer window sets it over this one."""
    monkeypatch.setattr(config, "_current", CFG)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # fast tests
    monkeypatch.setattr(couch, "ENTER_ATTEMPTS", 2)
    monkeypatch.setattr(couch, "READY_WAIT_S", 0.3)
    monkeypatch.setattr(couch, "WATCH_POLL_S", 0)


@pytest.fixture
def wire(rig, monkeypatch):
    """wire(script, default=None) replaces couch's seams. script =
    [(verb_prefix, reply-or-exception)], consumed in order and verb-checked.
    default handles unbounded polls (the READY wait is time-bound, not
    count-bound). Returns (log, exlink_calls)."""

    def _wire(script, default=None):
        log = CapturingLog("launch")
        sent = []

        def fake_exlink(name, **kw):
            sent.append(name)
            log("exlink_send", cmd=name, **kw)  # event ORDER proves the one rule

        def fake_ssh(cmd, timeout=15):
            if script:
                verb, reply = script.pop(0)
                assert cmd.startswith(verb), f"expected {verb!r}, couch sent {cmd!r}"
            elif default is not None:
                reply = default(cmd)
            else:
                raise AssertionError(f"unscripted ssh call: {cmd!r}")
            # BaseException, not Exception: KeyboardInterrupt is not an Exception,
            # and the narrower check returned it as a status string.
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(couch, "log", log)
        monkeypatch.setattr(couch, "exlink", fake_exlink)
        monkeypatch.setattr(gamepc, "ssh", fake_ssh)
        monkeypatch.setattr(couch, "wol", lambda: log("wol_sent"))
        monkeypatch.setattr(couch, "wait_port", lambda *a, **kw: True)
        return log, sent

    return _wire


@pytest.fixture
def tv_rig(rig, monkeypatch):
    """A rig with a tvIp: the set's own word is read, and the rescue's wait
    for it is shrunk to drill size."""
    monkeypatch.setattr(config, "_current", dict(CFG, tvIp="tv"))
    monkeypatch.setattr(couch, "TV_WAIT_S", 0.2)
    monkeypatch.setattr(couch, "TV_POKE_S", 0.05)


# --- atomic acquire: two racers, one winner, both paths -----------------------


@pytest.mark.parametrize(
    "seed_age", [None, sessionlock.LOCK_STALE_S + 60], ids=["empty", "stale"]
)
def test_acquire_has_exactly_one_winner(seed_age):
    for _ in range(25):
        seed_lock(seed_age)
        barrier, results = threading.Barrier(2), [None, None]

        def racer(i, barrier=barrier, results=results):
            barrier.wait()
            results[i] = sessionlock.acquire(f"t{i} {i}")

        threads = [threading.Thread(target=racer, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # The loser must ANSWER busy, not crash: Windows raises a sharing
        # violation, not FileExistsError. A crashed racer leaves None.
        assert sorted(results) == [False, True], (seed_age, results)


# --- ownership: release refuses a successor's lock ----------------------------


def test_release_refuses_a_successors_lock():
    assert sessionlock.acquire(f"ab12cd {os.getpid()}")
    assert sessionlock.release() and not sessionlock.lock_file().exists()
    seed_lock(10, content=f"ffffff {os.getpid() + 1}")  # someone else's
    assert not sessionlock.release() and sessionlock.lock_file().exists()
    seed_lock(10, content="1723500000.0")  # pre-note legacy
    assert sessionlock.release() and not sessionlock.lock_file().exists()


# --- happy path: ordering, the one rule, appid queue, lock lifecycle ----------


def test_happy_path_switches_the_input_only_after_ready(wire):
    sessionlock.last_error_file().write_text("old failure")  # success must supersede it
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("status", READY_TS),
            ("launch 777", "OK"),
            ("status", "NOTREADY"),  # watch: host ended the session
        ]
    )
    assert couch.start("777") == 0
    ev = log.events()
    assert ["launch_start", "wol_sent", "ssh_up", "enter_dispatched", "host_ready"] == [
        e
        for e in ev
        if e in ("launch_start", "wol_sent", "ssh_up", "enter_dispatched", "host_ready")
    ], ev
    switches = [
        i
        for i, r in enumerate(log.records)
        if r["event"] == "exlink_send" and r["cmd"] == "hdmi4"
    ]
    ready_at = ev.index("host_ready")
    assert switches and all(i > ready_at for i in switches), (ev, switches)
    assert sent == ["power_on", "hdmi4", "power_off"], sent
    assert "game_launch" in ev and "session_ended" in ev and "session_idle" in ev
    assert log.find("game_launch")[0]["level"] == "info"  # OK: a clean launch
    # A timestamp marker is the legacy shape (pre-turn-stamping PC): accepted,
    # but flagged unverified.
    assert log.find("host_ready")[0]["verified"] is False
    assert not sessionlock.lock_file().exists(), "session end must release the lock"
    assert not sessionlock.last_error_file().exists(), "success must clear last_error"


def test_already_is_a_degraded_launch_not_a_clean_one(wire):
    # ALREADY = the appid was already up from an earlier session: Big Picture
    # on the TV, undrivable (2026-08-13, turn 14852d). Level, not event.
    log, _ = wire(
        [
            ("enter", "OK"),
            ("status", READY_TS),
            ("launch 777", "ALREADY"),
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start("777") == 0
    assert log.find("game_launch")[0]["level"] == "warn", log.records


# --- READY generation identity: verified / foreign / converge -----------------


def test_a_marker_echoing_our_turn_is_verified_ready(wire):
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "ab12cd"),  # echoes OUR turn
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    assert log.find("host_ready")[0]["verified"] is True


def test_a_foreign_marker_is_waited_out_and_warned_once(wire):
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "ffffff"),  # stale marker, another life
            ("status", "ffffff"),  # (warned once, not per poll)
            ("status", "ab12cd"),  # our Enter overwrote it
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    assert len(log.find("ready_foreign")) == 1, log.records
    ready = log.find("host_ready")
    assert ready and ready[0]["verified"] is True
    switches = [r for r in log.find("exlink_send") if r["cmd"] == "hdmi4"]
    assert switches, "the launch must still complete once the marker is ours"


def test_watch_stands_down_when_a_successors_turn_is_in_the_marker(wire):
    assert sessionlock.acquire(f"ab12cd {os.getpid()}")
    log, sent = wire([("status", "eeeeee")])  # marker changed identity
    couch.watch(expected="ab12cd")
    ended = log.find("session_ended")
    assert ended and ended[0]["reason"] == "superseded", ended
    assert not sent, "a superseded watcher must not drive the TV"
    assert sessionlock.lock_file().exists(), "the lock is the successor's to release"


# --- busy and stale locks -----------------------------------------------------


def test_a_fresh_lock_refuses_before_any_side_effect(wire):
    seed_lock(10)
    log, sent = wire([])
    assert couch.start() == 1
    assert "launch_busy" in log.events() and not sent


def test_a_stale_lock_is_recycled_and_a_failing_enter_releases_it(wire):
    seed_lock(sessionlock.LOCK_STALE_S + 60)
    log, sent = wire([("enter", "FAILED:1"), ("enter", "FAILED:1")])
    assert couch.start() == 1
    ev = log.events()
    assert "lock_recycled" in ev and "launch_failed" in ev, ev
    assert sent == ["power_on", "power_off"], (
        f"failure restores power, not input: {sent}"
    )
    assert not sessionlock.lock_file().exists(), "failure must release the lock"
    assert "Enter" in sessionlock.last_error_file().read_text()
    # the refusal is logged once per distinct answer, not per retry
    assert [r["answer"] for r in log.find("enter_refused")] == ["FAILED:1"], log.records


def test_ready_never_appearing_gives_the_same_guarantees(wire):
    log, sent = wire([("enter", "OK")], default=lambda cmd: "NOTREADY")
    assert couch.start() == 1
    assert "launch_failed" in log.events()
    assert sent == ["power_on", "power_off"], sent
    assert (
        not sessionlock.lock_file().exists()
        and "READY" in sessionlock.last_error_file().read_text()
    )


# --- the TV-asleep rescue and the dead-Enter rescue ---------------------------


def test_the_tv_asleep_rescue_resends_power_on_once(wire, monkeypatch):
    # Default retry threshold sits past the 0.3 s test READY wait; inside it
    # the re-send appears once, however many times the poll spins.
    monkeypatch.setattr(couch, "WAKE_RETRY_S", 0.05)
    log, sent = wire([("enter", "OK")], default=lambda cmd: "NOTREADY")
    assert couch.start() == 1
    assert sent == ["power_on", "power_on", "power_off"], sent
    assert log.find("exlink_send")[1]["again"] is True, log.records


def test_an_enter_that_dies_mid_wait_is_repoked_and_redispatched(wire, monkeypatch):
    # TV acks power_on, stays dark, Enter gives up, K15 polls a dead task for
    # 120 s (2026-08-13/16/19). Two (NOTREADY, IDLE) pairs prove death.
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),  # twice = really dead
            ("enter", "OK"),  # so run it again
            ("status", "ab12cd"),  # the TV woke this time
            ("status", "NOTREADY"),  # watch(): session ends
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "enter_died" in ev and "enter_redispatched" in ev, ev
    assert log.find("host_ready")[0]["verified"] is True
    assert sent[:2] == ["power_on", "power_on"], (
        f"re-poke must precede the retry: {sent}"
    )
    assert log.find("exlink_send")[1]["again"] is True, log.records
    assert "hdmi4" in sent, "a rescued launch still switches the input"


def test_a_retry_that_dies_too_fails_now_not_at_the_window(wire, monkeypatch):
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    monkeypatch.setattr(couch, "READY_WAIT_S", 30)  # only the raise may end it
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),
            ("enter", "OK"),  # the one rescue
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),  # gone again
        ]
    )
    t0 = time.time()
    assert couch.start() == 1
    assert time.time() - t0 < 5, "a proven-dead Enter must not wait out the window"
    ev = log.events()
    assert ev.count("enter_died") == 2, ev
    assert ev.count("enter_redispatched") == 1, ev
    assert "launch_failed" in ev
    assert sent == ["power_on", "power_on", "power_off"], f"input untouched: {sent}"
    assert (
        not sessionlock.lock_file().exists()
        and "READY" in sessionlock.last_error_file().read_text()
    )


def test_one_idle_read_is_not_death(wire, monkeypatch):
    # The write-then-exit race: Enter writes the marker and THEN exits, so a
    # lone (NOTREADY, IDLE) pair can be those two instants read in the wrong
    # order.
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),  # looks dead...
            ("status", "ab12cd"),  # ...but the marker landed
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "enter_died" not in ev and "enter_redispatched" not in ev, ev
    assert log.find("host_ready")[0]["verified"] is True
    assert sent.count("power_on") == 1, f"no rescue was needed: {sent}"


@pytest.mark.parametrize(
    "reply", ["DENIED", RuntimeError("blip")], ids=["denied", "blip"]
)
def test_no_enterstate_information_is_not_death(wire, monkeypatch, reply):
    # A PC that predates the verb: enterstate answers DENIED on an un-deployed
    # gaming PC; an ssh blip raises. Both must leave the old timeout behaviour
    # unchanged - the short window must time out, not be rescued.
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    log, sent = wire(
        [("enter", "OK")],
        default=lambda cmd: reply if cmd.startswith("enterstate") else "NOTREADY",
    )
    assert couch.start() == 1
    ev = log.events()
    assert "enter_died" not in ev and "enter_redispatched" not in ev, ev
    assert "launch_failed" in ev, ev
    assert sent == ["power_on", "power_off"], sent
    assert not sessionlock.lock_file().exists()


# --- aborts: Ctrl-C and the voice cancel --------------------------------------


def test_ctrl_c_in_the_launch_console_is_an_abort(wire):
    # KeyboardInterrupt is a BaseException, so `except Exception` missed it: no
    # terminal event, no buzz, lock left to staleness (2026-08-16, turn b43b74).
    log, sent = wire([("enter", "OK"), ("status", KeyboardInterrupt())])
    assert couch.start() == 1
    ev = log.events()
    assert "launch_aborted" in ev and "launch_failed" not in ev, ev
    assert not sessionlock.lock_file().exists(), (
        "an aborted launch still releases the lock"
    )
    assert not sessionlock.last_error_file().exists(), (
        "a deliberate abort must not buzz the Puck"
    )
    assert sent == ["power_on", "power_off"], f"abort restores power, not input: {sent}"


def test_the_voice_cancel_marker_aborts_the_launch(wire, monkeypatch):
    # ssh `exit` only stops a RUNNING Enter, so it raced the redispatch rescue
    # (2026-08-21, turn 0b785e). The marker reaches THIS process.
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)  # only the cancel may end this wait

    def cancel_on_first_poll(cmd):
        if cmd.startswith("exit"):
            return "OK"  # the abort's own teardown dispatch
        sessionlock.cancel_file().write_text(
            "aaaaaa"
        )  # the cancelling utterance's turn
        return "NOTREADY"

    log, sent = wire([("enter", "OK")], default=cancel_on_first_poll)
    t0 = time.time()
    assert couch.start() == 1
    assert time.time() - t0 < 3, "a cancelled launch must not wait out the window"
    ev = log.events()
    assert "launch_aborted" in ev and "launch_failed" not in ev, ev
    aborted = log.find("launch_aborted")[0]
    assert aborted["err"] == "Cancelled", aborted
    assert aborted["cancelled_by"] == "aaaaaa", aborted
    # The abort dispatches its OWN exit: an Enter still inside the schtasks
    # trigger gap would otherwise run to completion with no watcher alive.
    exits = log.find("exit_dispatched")
    assert exits and exits[0]["reason"] == "cancel_after_enter", ev
    assert not sessionlock.lock_file().exists(), (
        "a cancelled launch still releases the lock"
    )
    assert not sessionlock.last_error_file().exists(), (
        "a cancel is deliberate - no fail buzz"
    )
    assert not sessionlock.cancel_file().exists(), (
        "consumed, or it kills the NEXT launch too"
    )
    # The voice teardown that left the TV lit for the night (2026-08-23 b540b9).
    assert sent == ["power_on", "power_off"], f"cancel restores power: {sent}"


def test_a_cancel_beats_the_rescue(wire, monkeypatch):
    # Cancel lands as the death is proven, too late for the loop-top check; the
    # idle_seen branch must catch it before the re-poke and the second Enter.
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    reads = {"n": 0}

    def die_then_cancel(cmd):
        if cmd.startswith("exit"):
            return "OK"
        if cmd.startswith("enterstate"):
            reads["n"] += 1
            if reads["n"] == 2:  # written as the death is proven
                sessionlock.cancel_file().write_text("bbbbbb")
            return "IDLE"
        return "NOTREADY"

    log, sent = wire([("enter", "OK")], default=die_then_cancel)
    assert couch.start() == 1
    ev = log.events()
    assert "enter_died" in ev, ev
    assert "enter_redispatched" not in ev, ev
    assert "launch_aborted" in ev and "launch_failed" not in ev, ev
    assert sent == ["power_on", "power_off"], f"no re-poke while tearing down: {sent}"
    assert (
        not sessionlock.lock_file().exists() and not sessionlock.cancel_file().exists()
    )


def test_a_stale_cancel_is_void(wire):
    # It predates this launch: nobody consumed it, and it is not our intent.
    sessionlock.cancel_file().write_text("ffffff")
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "ab12cd"),
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    assert "launch_aborted" not in log.events(), log.events()
    assert "host_ready" in log.events()


# --- TV evidence: rides the READY wait, gates only the rescue -----------------


def test_a_set_that_keeps_answering_standby_is_refusing(wire, tv_rig, monkeypatch):
    # Enter still goes out at once, but the rescue won't redispatch into the
    # dark and the fail names it.
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    monkeypatch.setattr(
        tv, "tv_power_state", lambda ip, timeout=2.0, raw=False: "standby"
    )
    log, sent = wire(
        [("enter", "OK")],
        default=lambda cmd: "IDLE" if cmd.startswith("enterstate") else "NOTREADY",
    )
    assert couch.start() == 1
    ev = log.events()
    assert "enter_dispatched" in ev, "Enter is never gated - zero happy-path tax"
    assert "enter_died" in ev and "enter_redispatched" not in ev, ev
    assert "launch_failed" in ev and "tv_on" not in ev, ev
    assert "TV never reported on" in sessionlock.last_error_file().read_text()
    assert log.find("launch_start")[0]["tv"] == "standby"
    assert set(sent) == {"power_on", "power_off"} and sent[-1] == "power_off", (
        f"the evidence re-pokes while not-on, and never switches input: {sent}"
    )
    assert not sessionlock.lock_file().exists()


def test_the_evidence_rides_the_ready_wait(wire, tv_rig, monkeypatch):
    # The healthy shape: Enter first, the set flips on mid-wait, launch lands.
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    monkeypatch.setattr(couch, "TV_POKE_S", 10)  # real seconds - no poke inside a drill
    seq = ["standby", "standby", "standby"]  # first pop = launch_start read
    monkeypatch.setattr(
        tv,
        "tv_power_state",
        lambda ip, timeout=2.0, raw=False: seq.pop(0) if seq else "on",
    )
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("status", "NOTREADY"),
            ("status", "ab12cd"),
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_on" in ev and "host_ready" in ev, ev
    assert ev.index("enter_dispatched") < ev.index("tv_on"), (
        "Enter must not wait for the set - the evidence rides the READY wait"
    )
    assert "hdmi4" in sent and sent.count("power_on") == 1, sent


def test_a_set_that_answered_on_lets_the_rescue_redispatch_at_once(
    wire, tv_rig, monkeypatch
):
    # Enter dies but the set HAD answered on: the rescue redispatches at once.
    monkeypatch.setattr(couch, "ENTER_SETTLE_S", 0)
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    monkeypatch.setattr(tv, "tv_power_state", lambda ip, timeout=2.0, raw=False: "on")
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),
            ("status", "NOTREADY"),
            ("enterstate", "IDLE"),
            ("enter", "OK"),
            ("status", "ab12cd"),
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_on" in ev and "enter_died" in ev and "enter_redispatched" in ev, ev
    assert log.find("host_ready")[0]["verified"] is True


def test_a_set_found_on_stays_on(wire, tv_rig, monkeypatch):
    # A set the viewer already had on is not the launch's to switch off: its
    # power_on was a no-op there, and the restore would end someone's show.
    monkeypatch.setattr(tv, "tv_power_state", lambda ip, timeout=2.0, raw=False: "on")
    log, sent = wire([("enter", "OK")], default=lambda cmd: "NOTREADY")
    assert couch.start() == 1
    assert "launch_failed" in log.events()
    assert sent == ["power_on"], f"a set found on stays on: {sent}"
    assert not sessionlock.lock_file().exists()


def test_a_set_that_cannot_be_read_stands_down_to_the_blind_path(
    wire, tv_rig, monkeypatch
):
    # A set that cannot be READ is not a refused one.
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)
    monkeypatch.setattr(tv, "tv_power_state", lambda ip, timeout=2.0, raw=False: None)
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "NOTREADY"),
            ("status", "NOTREADY"),
            ("status", "NOTREADY"),
            ("status", "ab12cd"),
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_state_unknown" in ev and "host_ready" in ev, ev
    # A sentinel, not None: events.emit drops None-valued fields, so an
    # unreachable set would look identical to a rig with no tvIp.
    assert log.find("launch_start")[0]["tv"] == "unreachable"


def test_no_tvip_means_no_gate(wire, monkeypatch):
    # The gate must not exist - not a read, not a field.
    monkeypatch.setattr(couch, "READY_WAIT_S", 5)

    def boom(ip, timeout=2.0, raw=False):
        raise AssertionError("no tvIp - the launch must never read the TV")

    monkeypatch.setattr(tv, "tv_power_state", boom)
    log, sent = wire(
        [
            ("enter", "OK"),
            ("status", "ab12cd"),
            ("status", "NOTREADY"),
        ]
    )
    assert couch.start(turn="ab12cd") == 0
    assert "tv" not in log.find("launch_start")[0], log.find("launch_start")


# --- watch: blips forgiven, a run of failures dies honestly -------------------


def test_watch_forgives_blips_and_dies_on_a_run_of_failures(wire):
    assert sessionlock.acquire(f"ab12cd {os.getpid()}")
    log, sent = wire(
        [
            ("status", RuntimeError("blip")),
            ("status", RuntimeError("blip")),
            ("status", READY_TS),  # recovery resets the counter
            ("status", RuntimeError("down")),
            ("status", RuntimeError("down")),
            ("status", RuntimeError("down")),  # third consecutive = dead
            ("exit", "OK"),  # best-effort Puck release
        ]
    )
    couch.watch()
    ended = log.find("session_ended")
    assert ended and ended[0]["reason"] == "ssh_fails", ended
    assert "exit_dispatched" in log.events()
    assert sent == ["power_off"] and not sessionlock.lock_file().exists()


# --- reconcile: resume live, clear dead (TV untouched), ride out errors -------


def test_reconcile_resumes_a_live_session(wire):
    seed_lock(60, content="ffffff 99999")  # dead owner's note
    log, sent = wire([("status", READY_TS), ("status", "NOTREADY")])
    assert couch.reconcile() == 0
    assert "reconcile_resumed" in log.events() and "session_idle" in log.events()
    assert sent == ["power_off"] and not sessionlock.lock_file().exists()


def test_reconcile_clears_a_dead_session_without_touching_the_tv(wire):
    seed_lock(60)
    log, sent = wire([("status", "NOTREADY")])
    assert couch.reconcile() == 0
    assert log.find("reconcile_cleared")[0]["reason"] == "dead_session"
    assert not sent, "a dead session's reconcile must not drive the TV"
    assert not sessionlock.lock_file().exists()


def test_reconcile_clears_an_unreachable_host(wire):
    seed_lock(60)
    log, sent = wire(
        [
            ("status", RuntimeError("boot")),
            ("status", RuntimeError("boot")),
            ("status", RuntimeError("boot")),
        ]
    )
    assert couch.reconcile() == 0
    # never answered is not the same as answered NOTREADY
    assert log.find("reconcile_cleared")[0]["reason"] == "unreachable"
    assert not sessionlock.lock_file().exists()


def test_reconcile_with_no_lock_does_nothing(wire):
    seed_lock(None)
    log, sent = wire([])
    assert couch.reconcile() == 0 and not sent  # no lock: nothing to do


# --- a config doctor would FAIL is refused before the lock and the TV ---------


def test_an_invalid_config_is_refused_before_the_lock_and_the_tv(wire, monkeypatch):
    log, sent = wire([])
    monkeypatch.setattr(
        config, "_current", {k: v for k, v in CFG.items() if k != "gamingPcMac"}
    )
    assert couch.start() == 2
    inv = log.find("config_invalid")
    assert inv and inv[0]["missing"] == ["gamingPcMac"], inv
    assert not sent and "launch_start" not in log.events()
    assert (
        not sessionlock.lock_file().exists()
        and "gamingPcMac" in sessionlock.last_error_file().read_text()
    )
