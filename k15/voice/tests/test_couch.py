"""Blind test: couch.py's orchestration state machine, with every side effect
stubbed at its seam (ssh, exlink, wol, wait_port). Run:
    .venv\\Scripts\\python tests\\test_couch.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import _bootstrap                               # noqa: F401,E402
from _bootstrap import fresh_state              # noqa: E402

import cglib
import tv
import events

import couch
import gamepc

CFG = {"tvComPort": "COMX", "tvGamingCmd": "hdmi4", "tvIdleCmd": "hdmi1",
       "tvOffWhenDone": True, "sshHost": "gamepc",
       "gamingPcIp": "127.0.0.1", "gamingPcMac": "00-00-00-00-00-00"}

READY_TS = "2026-08-12T20:00:00"                 # what a legacy marker answers


def wire(script, default=None):
    """Replace couch's seams. script = [(verb_prefix, reply-or-exception)],
    consumed in order and verb-checked. default handles unbounded polls (the
    READY wait is time-bound, not count-bound). Returns (log, exlink_calls)."""
    log = cglib.CapturingLog("launch")
    couch.log = log
    sent = []

    def fake_exlink(name, **kw):
        sent.append(name)
        log("exlink_send", cmd=name, **kw)       # event ORDER proves the one rule

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

    couch.exlink = fake_exlink
    gamepc.ssh = fake_ssh
    couch.wol = lambda: log("wol_sent")
    couch.wait_port = lambda *a, **kw: True
    return log, sent


def main():
    real_sleep = time.sleep
    time.sleep = lambda s: None                  # fast tests
    cglib.use_config(CFG)
    couch.ENTER_ATTEMPTS = 2
    couch.READY_WAIT_S = 0.3
    couch.WATCH_POLL_S = 0

    # --- atomic acquire: two racers, one winner, both paths -------------------
    for seed_age in (None, cglib.LOCK_STALE_S + 60):     # empty, then stale
        for _ in range(25):
            fresh_state(seed_age)
            barrier, results = threading.Barrier(2), [None, None]

            def racer(i):
                barrier.wait()
                results[i] = cglib.acquire_lock(f"t{i} {i}")

            threads = [threading.Thread(target=racer, args=(i,)) for i in (0, 1)]
            for t in threads: t.start()
            for t in threads: t.join()
            # The loser must ANSWER busy, not crash: Windows raises a sharing
            # violation, not FileExistsError. A crashed racer leaves None.
            assert sorted(results) == [False, True], (seed_age, results)
    print("  acquire: 50 two-way races (empty + stale recycle), one winner each")

    # --- ownership: release refuses a successor's lock ------------------------
    fresh_state()
    assert cglib.acquire_lock(f"ab12cd {os.getpid()}")
    assert cglib.release_lock() and not cglib.LOCK.exists()
    fresh_state(10, lock_content=f"ffffff {os.getpid() + 1}")   # someone else's
    assert not cglib.release_lock() and cglib.LOCK.exists()
    fresh_state(10, lock_content="1723500000.0")                # pre-note legacy
    assert cglib.release_lock() and not cglib.LOCK.exists()
    print("  release: unlinks own + legacy locks, refuses a successor's")

    # --- happy path: ordering, the one rule, appid queue, lock lifecycle ------
    fresh_state()
    cglib.LAST_ERROR.write_text("old failure")   # success must supersede it
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"),
        ("status", READY_TS),
        ("launch 777", "OK"),
        ("status", "NOTREADY"),                  # watch: host ended the session
    ])
    assert couch.start("777") == 0
    ev = log.events()
    assert ["launch_start", "wol_sent", "ssh_up", "enter_dispatched",
            "host_ready"] == [e for e in ev if e in (
                "launch_start", "wol_sent", "ssh_up", "enter_dispatched",
                "host_ready")], ev
    switches = [i for i, r in enumerate(log.records)
                if r["event"] == "exlink_send" and r["cmd"] == "hdmi4"]
    ready_at = ev.index("host_ready")
    assert switches and all(i > ready_at for i in switches), (ev, switches)
    assert sent == ["power_on", "hdmi4", "power_off"], sent
    assert "game_launch" in ev and "session_ended" in ev and "session_idle" in ev
    assert log.find("game_launch")[0]["level"] == "info"      # OK: a clean launch
    # A timestamp marker is the legacy shape (pre-turn-stamping PC): accepted,
    # but flagged unverified.
    assert log.find("host_ready")[0]["verified"] is False
    assert not cglib.LOCK.exists(), "session end must release the lock"
    assert not cglib.LAST_ERROR.exists(), "success must clear last_error"
    print("  start: input switch strictly after READY, appid queued, lock released")

    # --- ALREADY is a degraded launch, not a clean one -----------------------
    # ALREADY = the appid was already up from an earlier session: Big Picture
    # on the TV, undrivable (2026-08-13, turn 14852d). Level, not event.
    fresh_state()
    log, _ = wire([
        ("enter", "OK"),
        ("status", READY_TS),
        ("launch 777", "ALREADY"),
        ("status", "NOTREADY"),
    ])
    assert couch.start("777") == 0
    assert log.find("game_launch")[0]["level"] == "warn", log.records
    print("  start: ALREADY reads as degraded, not as a launch that worked")

    # --- READY generation identity: verified / foreign / converge -------------
    fresh_state()
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ab12cd"),                    # echoes OUR turn
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    assert log.find("host_ready")[0]["verified"] is True
    print("  ready: a marker echoing our turn is verified")

    # --- watch: a successor's turn in the marker means stand down -------------
    fresh_state()
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ffffff"),                    # stale marker, another life
        ("status", "ffffff"),                    # (warned once, not per poll)
        ("status", "ab12cd"),                    # our Enter overwrote it
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    assert len(log.find("ready_foreign")) == 1, log.records
    ready = log.find("host_ready")
    assert ready and ready[0]["verified"] is True
    switches = [r for r in log.find("exlink_send") if r["cmd"] == "hdmi4"]
    assert switches, "the launch must still complete once the marker is ours"
    print("  ready: a FOREIGN marker is waited out, never switched to")

    fresh_state()
    assert cglib.acquire_lock(f"ab12cd {os.getpid()}")
    log, sent = wire([("status", "eeeeee")])     # marker changed identity
    couch.watch(expected="ab12cd")
    ended = log.find("session_ended")
    assert ended and ended[0]["reason"] == "superseded", ended
    assert not sent, "a superseded watcher must not drive the TV"
    assert cglib.LOCK.exists(), "the lock is the successor's to release"
    cglib.LOCK.unlink()
    print("  watch: superseded by a successor -> hands off TV and lock")

    # --- busy: fresh lock refuses before any side effect -----------------------
    fresh_state(10)
    log, sent = wire([])
    assert couch.start() == 1
    assert "launch_busy" in log.events() and not sent
    print("  busy: fresh lock refuses with zero side effects")

    # --- stale lock: recycled, then a failing Enter releases it ---------------
    fresh_state(cglib.LOCK_STALE_S + 60)
    log, sent = wire([("enter", "FAILED:1"), ("enter", "FAILED:1")])
    assert couch.start() == 1
    ev = log.events()
    assert "lock_recycled" in ev and "launch_failed" in ev, ev
    assert sent == ["power_on"], f"failure must never switch the input: {sent}"
    assert not cglib.LOCK.exists(), "failure must release the lock"
    assert "Enter" in cglib.LAST_ERROR.read_text()
    # the refusal is logged once per distinct answer, not per retry
    assert [r["answer"] for r in log.find("enter_refused")] == ["FAILED:1"], log.records
    print("  stale: recycled; failed Enter -> lock released, last_error, TV alone")

    # --- READY never appears: same guarantees ---------------------------------
    fresh_state()
    log, sent = wire([("enter", "OK")], default=lambda cmd: "NOTREADY")
    assert couch.start() == 1
    assert "launch_failed" in log.events()
    assert sent == ["power_on"], sent
    assert not cglib.LOCK.exists() and "READY" in cglib.LAST_ERROR.read_text()
    print("  no READY: timed out, lock released, input untouched")

    # --- the TV-asleep rescue: a second power_on, and only ever one -----------
    # Default retry threshold sits past the 0.3 s test READY wait; inside it
    # the re-send appears once, however many times the poll spins.
    fresh_state()
    couch.WAKE_RETRY_S = 0.05
    log, sent = wire([("enter", "OK")], default=lambda cmd: "NOTREADY")
    assert couch.start() == 1
    assert sent == ["power_on", "power_on"], sent
    assert log.find("exlink_send")[1]["again"] is True, log.records
    couch.WAKE_RETRY_S = 10                # leave no trap for the next case
    print("  wake retry: one extra power_on inside the READY wait, input alone")

    # --- Enter dies mid-wait: detected, re-poked, re-dispatched ---------------
    # TV acks power_on, stays dark, Enter gives up, K15 polls a dead task for
    # 120 s (2026-08-13/16/19). Two (NOTREADY, IDLE) pairs prove death.
    fresh_state()
    couch.ENTER_SETTLE_S = 0
    couch.READY_WAIT_S = 5
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),   # twice = really dead
        ("enter", "OK"),                                  # so run it again
        ("status", "ab12cd"),                             # the TV woke this time
        ("status", "NOTREADY"),                           # watch(): session ends
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "enter_died" in ev and "enter_redispatched" in ev, ev
    assert log.find("host_ready")[0]["verified"] is True
    assert sent[:2] == ["power_on", "power_on"], f"re-poke must precede the retry: {sent}"
    assert log.find("exlink_send")[1]["again"] is True, log.records
    assert "hdmi4" in sent, "a rescued launch still switches the input"
    print("  enter died: two idle reads -> re-poke + re-dispatch -> launch rescued")

    # --- the retry dies too: fail NOW, do not sit out the window --------------
    fresh_state()
    couch.READY_WAIT_S = 30                # long enough that only the raise ends it
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),
        ("enter", "OK"),                                  # the one rescue
        ("status", "NOTREADY"), ("enterstate", "IDLE"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),   # gone again
    ])
    t0 = time.time()
    assert couch.start() == 1
    assert time.time() - t0 < 5, "a proven-dead Enter must not wait out the window"
    ev = log.events()
    assert ev.count("enter_died") == 2, ev
    assert ev.count("enter_redispatched") == 1, ev
    assert "launch_failed" in ev
    assert sent == ["power_on", "power_on"], f"input untouched on failure: {sent}"
    assert not cglib.LOCK.exists() and "READY" in cglib.LAST_ERROR.read_text()
    print("  retry dies too: one rescue, then an immediate honest failure")

    # --- the write-then-exit race: one idle read is not death -----------------
    # Enter writes the marker and THEN exits, so a lone (NOTREADY, IDLE) pair
    # can be those two instants read in the wrong order.
    fresh_state()
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),   # looks dead...
        ("status", "ab12cd"),                             # ...but the marker landed
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "enter_died" not in ev and "enter_redispatched" not in ev, ev
    assert log.find("host_ready")[0]["verified"] is True
    assert sent.count("power_on") == 1, f"no rescue was needed: {sent}"
    print("  enter idle once: race re-read wins, no re-dispatch over a live session")

    # --- a PC that predates the verb: no information is not death -------------
    # enterstate answers DENIED on an un-deployed gaming PC; an ssh blip
    # raises. Both must leave the old timeout behaviour unchanged.
    couch.READY_WAIT_S = 0.3               # short window: must time out, not
                                           # be rescued
    for label, reply in (("DENIED", "DENIED"), ("blip", RuntimeError("blip"))):
        fresh_state()
        log, sent = wire([("enter", "OK")], default=lambda cmd, r=reply:
                         r if cmd.startswith("enterstate") else "NOTREADY")
        assert couch.start() == 1
        ev = log.events()
        assert "enter_died" not in ev and "enter_redispatched" not in ev, (label, ev)
        assert "launch_failed" in ev, (label, ev)
        assert sent == ["power_on"], (label, sent)
        assert not cglib.LOCK.exists(), label
    couch.ENTER_SETTLE_S = 10              # leave no trap for the next case
    couch.READY_WAIT_S = 0.3
    print("  enterstate unknown (old PC, ssh blip): no re-dispatch, timeout unchanged")

    # --- Ctrl-C in the launch console is not an Exception ----------------------
    # KeyboardInterrupt is a BaseException, so `except Exception` missed it: no
    # terminal event, no buzz, lock left to staleness (2026-08-16, turn b43b74).
    fresh_state()
    log, sent = wire([("enter", "OK"), ("status", KeyboardInterrupt())])
    assert couch.start() == 1
    ev = log.events()
    assert "launch_aborted" in ev and "launch_failed" not in ev, ev
    assert not cglib.LOCK.exists(), "an aborted launch still releases the lock"
    assert not cglib.LAST_ERROR.exists(), "a deliberate abort must not buzz the Puck"
    assert sent == ["power_on"], f"an abort must never switch the input: {sent}"
    print("  ctrl-C: launch_aborted, lock released, no fail buzz, TV alone")

    # --- voice cancel: end_session's marker aborts the launch ------------------
    # ssh `exit` only stops a RUNNING Enter, so it raced the redispatch rescue
    # (2026-08-21, turn 0b785e). The marker reaches THIS process.
    fresh_state()
    couch.READY_WAIT_S = 5                 # only the cancel may end this wait

    def cancel_on_first_poll(cmd):
        if cmd.startswith("exit"):
            return "OK"                    # the abort's own teardown dispatch
        cglib.CANCEL.write_text("aaaaaa")  # the cancelling utterance's turn
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
    assert not cglib.LOCK.exists(), "a cancelled launch still releases the lock"
    assert not cglib.LAST_ERROR.exists(), "a cancel is deliberate - no fail buzz"
    assert not cglib.CANCEL.exists(), "consumed, or it kills the NEXT launch too"
    assert sent == ["power_on"], f"a cancel must never switch the input: {sent}"
    print("  cancel: marker aborts the wait, consumed, no buzz, TV alone, "
          "exit chases the sent Enter")

    # --- cancel beats the rescue: no redispatch over a teardown ----------------
    # Cancel lands as the death is proven, too late for the loop-top check; the
    # idle_seen branch must catch it before the re-poke and the second Enter.
    fresh_state()
    couch.ENTER_SETTLE_S = 0
    reads = {"n": 0}

    def die_then_cancel(cmd):
        if cmd.startswith("exit"):
            return "OK"
        if cmd.startswith("enterstate"):
            reads["n"] += 1
            if reads["n"] == 2:            # written as the death is proven
                cglib.CANCEL.write_text("bbbbbb")
            return "IDLE"
        return "NOTREADY"

    log, sent = wire([("enter", "OK")], default=die_then_cancel)
    assert couch.start() == 1
    ev = log.events()
    assert "enter_died" in ev, ev
    assert "enter_redispatched" not in ev, ev
    assert "launch_aborted" in ev and "launch_failed" not in ev, ev
    assert sent == ["power_on"], f"no re-poke for a launch being torn down: {sent}"
    assert not cglib.LOCK.exists() and not cglib.CANCEL.exists()
    couch.ENTER_SETTLE_S = 10
    print("  cancel vs rescue: enter_died then cancel -> no redispatch, abort")

    # --- a stale cancel is void: it predates this launch -----------------------
    fresh_state()
    couch.READY_WAIT_S = 0.3
    cglib.CANCEL.write_text("ffffff")      # nobody consumed it; not our intent
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    assert "launch_aborted" not in log.events(), log.events()
    assert "host_ready" in log.events()
    print("  stale cancel: voided at start, launch unharmed")

    # --- TV evidence: rides the READY wait, gates only the rescue --------------
    real_tv_state = tv.tv_power_state
    cglib.use_config(dict(CFG, tvIp="tv"))
    couch.TV_WAIT_S = 0.2
    couch.TV_POKE_S = 0.05

    # A set that keeps answering standby is refusing: Enter still goes out at
    # once, but the rescue won't redispatch into the dark and the fail names it.
    fresh_state()
    couch.ENTER_SETTLE_S = 0
    couch.READY_WAIT_S = 5
    tv.tv_power_state = lambda ip, timeout=2.0, raw=False: "standby"
    log, sent = wire([("enter", "OK")], default=lambda cmd:
                     "IDLE" if cmd.startswith("enterstate") else "NOTREADY")
    assert couch.start() == 1
    ev = log.events()
    assert "enter_dispatched" in ev, "Enter is never gated - zero happy-path tax"
    assert "enter_died" in ev and "enter_redispatched" not in ev, ev
    assert "launch_failed" in ev and "tv_on" not in ev, ev
    assert "TV never reported on" in cglib.LAST_ERROR.read_text()
    assert log.find("launch_start")[0]["tv"] == "standby"
    assert set(sent) == {"power_on"} and len(sent) >= 2, \
        f"the evidence re-pokes while not-on, and never switches input: {sent}"
    assert not cglib.LOCK.exists()
    print("  tv evidence: standby set -> no redispatch into the dark, TV named")

    # The healthy shape: Enter first, the set flips on mid-wait, launch lands.
    fresh_state()
    couch.ENTER_SETTLE_S = 10
    couch.READY_WAIT_S = 5
    couch.TV_POKE_S = 10                   # real seconds - no poke inside a drill
    seq = ["standby", "standby", "standby"]          # first pop = launch_start read
    tv.tv_power_state = \
        lambda ip, timeout=2.0, raw=False: (seq.pop(0) if seq else "on")
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"),
        ("status", "NOTREADY"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_on" in ev and "host_ready" in ev, ev
    assert ev.index("enter_dispatched") < ev.index("tv_on"), \
        "Enter must not wait for the set - the evidence rides the READY wait"
    assert "hdmi4" in sent and sent.count("power_on") == 1, sent
    print("  tv evidence: Enter immediately, tv_on mid-wait, launch lands")

    # Enter dies but the set HAD answered on: the rescue redispatches at once.
    fresh_state()
    couch.ENTER_SETTLE_S = 0
    tv.tv_power_state = lambda ip, timeout=2.0, raw=False: "on"
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),
        ("status", "NOTREADY"), ("enterstate", "IDLE"),
        ("enter", "OK"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_on" in ev and "enter_died" in ev and "enter_redispatched" in ev, ev
    assert log.find("host_ready")[0]["verified"] is True
    print("  tv evidence: confirmed-on set -> rescue redispatches at once")

    # A set that cannot be READ is not a refused one: stand down to the legacy
    # blind path.
    fresh_state()
    couch.ENTER_SETTLE_S = 10
    tv.tv_power_state = lambda ip, timeout=2.0, raw=False: None
    log, sent = wire([
        ("enter", "OK"),
        ("status", "NOTREADY"),
        ("status", "NOTREADY"),
        ("status", "NOTREADY"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_state_unknown" in ev and "host_ready" in ev, ev
    # A sentinel, not None: events.emit drops None-valued fields, so an
    # unreachable set would look identical to a rig with no tvIp.
    assert log.find("launch_start")[0]["tv"] == "unreachable"
    print("  tv evidence: unreadable set -> stand down, legacy launch unharmed")

    # No tvIp: the gate must not exist - not a read, not a field.
    fresh_state()

    def boom(ip, timeout=2.0, raw=False):
        raise AssertionError("no tvIp - the launch must never read the TV")
    tv.tv_power_state = boom
    cglib.use_config(CFG)
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    assert "tv" not in log.find("launch_start")[0], log.find("launch_start")
    tv.tv_power_state = real_tv_state
    print("  tv gate: no tvIp -> no reads, launch exactly as before")

    # --- watch: blips forgiven, a run of failures dies honestly ---------------
    fresh_state()
    assert cglib.acquire_lock(f"ab12cd {os.getpid()}")
    log, sent = wire([
        ("status", RuntimeError("blip")),
        ("status", RuntimeError("blip")),
        ("status", READY_TS),                    # recovery resets the counter
        ("status", RuntimeError("down")),
        ("status", RuntimeError("down")),
        ("status", RuntimeError("down")),        # third consecutive = dead
        ("exit", "OK"),                          # best-effort Puck release
    ])
    couch.watch()
    ended = log.find("session_ended")
    assert ended and ended[0]["reason"] == "ssh_fails", ended
    assert "exit_dispatched" in log.events()
    assert sent == ["power_off"] and not cglib.LOCK.exists()
    print("  watch: 2 blips forgiven, 3 straight deaths -> exit + TV restore")

    # --- reconcile: resume live, clear dead (TV untouched), ride out errors ---
    fresh_state(60, lock_content="ffffff 99999")         # dead owner's note
    log, sent = wire([("status", READY_TS), ("status", "NOTREADY")])
    assert couch.reconcile() == 0
    assert "reconcile_resumed" in log.events() and "session_idle" in log.events()
    assert sent == ["power_off"] and not cglib.LOCK.exists()

    fresh_state(60)
    log, sent = wire([("status", "NOTREADY")])
    assert couch.reconcile() == 0
    assert log.find("reconcile_cleared")[0]["reason"] == "dead_session"
    assert not sent, "a dead session's reconcile must not drive the TV"
    assert not cglib.LOCK.exists()

    fresh_state(60)
    log, sent = wire([("status", RuntimeError("boot")),
                      ("status", RuntimeError("boot")),
                      ("status", RuntimeError("boot"))])
    assert couch.reconcile() == 0
    # never answered is not the same as answered NOTREADY
    assert log.find("reconcile_cleared")[0]["reason"] == "unreachable"
    assert not cglib.LOCK.exists()

    fresh_state(None)
    log, sent = wire([])
    assert couch.reconcile() == 0 and not sent   # no lock: nothing to do
    print("  reconcile: resumes live, clears dead with TV untouched, "
          "shrugs off boot-time errors")

    time.sleep = real_sleep
    # --- a config doctor would FAIL is refused before the lock and the TV ------
    fresh_state()
    log, sent = wire([])
    cglib.use_config({k: v for k, v in CFG.items() if k != "gamingPcMac"})
    try:
        assert couch.start() == 2
    finally:
        cglib.use_config(CFG)
    inv = log.find("config_invalid")
    assert inv and inv[0]["missing"] == ["gamingPcMac"], inv
    assert not sent and "launch_start" not in log.events()
    assert not cglib.LOCK.exists() and "gamingPcMac" in cglib.LAST_ERROR.read_text()
    print("  config: a missing key is config_invalid + exit 2, before the lock and the TV")

    print("OK - couch: atomic acquire, ownership, one-rule ordering, failure "
          "release, watch death, reconcile paths")


if __name__ == "__main__":
    main()
