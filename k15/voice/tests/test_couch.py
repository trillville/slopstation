"""Blind test: couch.py's orchestration state machine, with every side effect
stubbed at its documented seam (ssh, exlink, wol, wait_port). What it pins:

  * the lock is taken ATOMICALLY - two racers, exactly one winner - and
    release refuses a successor's lock;
  * READY identity: our turn verifies, a foreign one is waited out, a
    timestamp is legacy-accepted, and a changed one supersedes the watcher;
  * the one rule - the TV input switch happens only after READY, never on a
    failure path, which also leaves last_error for the listener;
  * the TV-asleep rescue: exactly one extra power_on inside the READY wait,
    and it never becomes an input switch;
  * the Enter-died rescue: two idle reads (never one - that is the
    write-then-exit race) buy one re-poke and one re-dispatch, a second death
    fails immediately rather than waiting out the window, and a PC that cannot
    answer `enterstate` at all falls back to exactly the old timeout;
  * Ctrl-C is a BaseException: it still releases the lock, and deliberately
    does NOT leave last_error for the listener to buzz;
  * a voice cancel (state/cancel) aborts the same way, is consumed rather
    than left for the next launch, beats the enter_redispatched rescue even
    when it lands as the death is being proven, and a STALE one is voided
    at launch start;
  * the TV-wake gate: Enter is not dispatched until the set REPORTS on,
    a set that keeps answering standby fails the launch early with the TV
    named, an UNREADABLE set falls open to the legacy blind path, and a rig
    with no tvIp never reads at all;
  * watch() rides out ssh blips and dies honestly on a run of them;
  * reconcile resumes a live session and clears a dead one, TV untouched.

Run:
    .venv\\Scripts\\python tests\\test_couch.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cglib
import events

events.LOG_DIR = Path(tempfile.mkdtemp())        # keep test events out of logs/

import couch                                     # noqa: E402  (needs LOG_DIR set)

CFG = {"tvComPort": "COMX", "tvGamingCmd": "hdmi4", "tvIdleCmd": "hdmi1",
       "tvOffWhenDone": True, "sshHost": "gamepc",
       "gamingPcIp": "127.0.0.1", "gamingPcMac": "00-00-00-00-00-00"}

READY_TS = "2026-08-12T20:00:00"                 # what a legacy marker answers


def fresh_state(lock_age_s=None, lock_content="x"):
    """Point cglib's lock + last_error into a new tmpdir. lock_age_s seeds a
    lock of that age (None = absent)."""
    tmp = Path(tempfile.mkdtemp())
    cglib.LOCK = tmp / "session.lock"
    cglib.LAST_ERROR = tmp / "last_error"
    cglib.CANCEL = tmp / "cancel"
    if lock_age_s is not None:
        cglib.LOCK.write_text(lock_content)
        old = time.time() - lock_age_s
        os.utime(cglib.LOCK, (old, old))
    return tmp


def wire(script, default=None):
    """Replace couch's seams. script = [(verb_prefix, reply-or-exception)],
    consumed in order and verb-checked, so the test dies loudly the moment
    couch.py's call sequence drifts. default handles unbounded polls (the
    READY wait is time-bound, not count-bound). Returns (log, exlink_calls)."""
    log = cglib.CapturingLog("launch")
    couch.log = log
    sent = []

    def fake_exlink(name, **kw):
        sent.append(name)
        log("exlink_send", cmd=name, **kw)       # so event ORDER proves the one rule

    def fake_ssh(cmd, timeout=15):
        if script:
            verb, reply = script.pop(0)
            assert cmd.startswith(verb), f"expected {verb!r}, couch sent {cmd!r}"
        elif default is not None:
            reply = default(cmd)
        else:
            raise AssertionError(f"unscripted ssh call: {cmd!r}")
        # BaseException, not Exception: KeyboardInterrupt is the one this
        # harness most needs to be able to express, and it is not an Exception
        # - the narrower check quietly RETURNED it as a status string instead.
        if isinstance(reply, BaseException):
            raise reply
        return reply

    couch.exlink = fake_exlink
    couch.ssh = fake_ssh
    couch.wol = lambda: log("wol_sent")
    couch.wait_port = lambda *a, **kw: True
    return log, sent


def main():
    real_sleep = time.sleep
    time.sleep = lambda s: None                  # fast tests
    couch.CFG = CFG
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
            # One True AND one False: the loser must ANSWER busy, not crash.
            # Windows raises a sharing violation, not FileExistsError; a
            # crashed racer would leave None here.
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
    # A timestamp marker is the LEGACY shape (pre-turn-stamping PC): accepted
    # so either machine can deploy first, but flagged as unverified.
    assert log.find("host_ready")[0]["verified"] is False
    assert not cglib.LOCK.exists(), "session end must release the lock"
    assert not cglib.LAST_ERROR.exists(), "success must clear last_error"
    print("  start: input switch strictly after READY, appid queued, lock released")

    # --- ALREADY is a degraded launch, not a clean one -----------------------
    # The PC answers ALREADY when the appid is already up from an EARLIER
    # session, which is the shape that leaves Big Picture on the TV and
    # undrivable from the couch (2026-08-13, turn 14852d). It read as a
    # flawless launch from here. Asserting on the LEVEL rather than the event
    # is the exception the interface rule earns: renaming game_launch already
    # breaks the happy path above, but sliding back to info would be silent,
    # and info is exactly the bug.
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
    # Every case above sends power_on exactly once because the default retry
    # threshold sits far past the 0.3 s test READY wait. Bring it inside that
    # window and the re-send appears - once, however many times the poll spins,
    # which is the whole point of the latch.
    fresh_state()
    couch.WAKE_RETRY_S = 0.05
    log, sent = wire([("enter", "OK")], default=lambda cmd: "NOTREADY")
    assert couch.start() == 1
    assert sent == ["power_on", "power_on"], sent
    assert log.find("exlink_send")[1]["again"] is True, log.records
    couch.WAKE_RETRY_S = 10                # leave no trap for the next case
    print("  wake retry: one extra power_on inside the READY wait, input alone")

    # --- Enter dies mid-wait: detected, re-poked, re-dispatched ---------------
    # The failure this exists for: on 2026-08-13, 08-16 and 08-19 the TV acked
    # power_on, stayed dark, Enter gave up on the profile, and the K15 spent
    # the rest of its 120 s polling a task that had already exited. Two
    # (NOTREADY, IDLE) pairs are the evidence of death; a second power_on and a
    # second Enter are the rescue.
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
    # The bound on the whole scheme. Once the rescue Enter is also proven gone
    # there is nothing left to wait for, and the fail buzz should reach the
    # couch while whoever pressed the chord is still holding the controller.
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
    # can be those two instants seen in the wrong order. Re-dispatching there
    # would tear down the session that had just come up.
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
    # enterstate answers DENIED on an un-deployed gaming PC, and an ssh blip
    # raises. Both must leave the old timeout behaviour exactly as it was -
    # reading either as "Enter is dead" would fight a healthy launch.
    couch.READY_WAIT_S = 0.3               # back to the short window: this case
                                           # must time out, not be rescued
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
    # 2026-08-16 turn b43b74: dispatched Enter, then emitted nothing ever again
    # - no terminal event, no fail buzz, and a lock left for staleness to
    # recycle. KeyboardInterrupt is a BaseException, so `except Exception`
    # never saw it. No last_error on purpose: whoever pressed the key knows.
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
    # 2026-08-21 turn 0b785e: "end the session" against an in-flight launch
    # could only ssh `exit`, which stops a RUNNING Enter - the first Enter had
    # already died, so the exit raced the enter_redispatched rescue and won on
    # timing alone. The marker is the channel that reaches THIS process.
    fresh_state()
    couch.READY_WAIT_S = 5                 # only the cancel may end this wait

    def cancel_on_first_poll(cmd):
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
    assert not cglib.LOCK.exists(), "a cancelled launch still releases the lock"
    assert not cglib.LAST_ERROR.exists(), "a cancel is deliberate - no fail buzz"
    assert not cglib.CANCEL.exists(), "consumed, or it kills the NEXT launch too"
    assert sent == ["power_on"], f"a cancel must never switch the input: {sent}"
    print("  cancel: marker aborts the wait, consumed, no buzz, TV alone")

    # --- cancel beats the rescue: no redispatch over a teardown ----------------
    # The exact interleave from 0b785e: the cancel lands while the death is
    # being proven, one iteration too late for the loop-top check. The
    # last-instant check inside the idle_seen branch is what must catch it -
    # before the re-poke, before the second Enter.
    fresh_state()
    couch.ENTER_SETTLE_S = 0
    reads = {"n": 0}

    def die_then_cancel(cmd):
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

    # --- the TV-wake gate: Enter waits for the set's own word ------------------
    real_tv_state = cglib.tv_power_state
    couch.CFG = dict(CFG, tvIp="tv")
    couch.TV_WAIT_S = 0.2
    couch.TV_POKE_S = 0.05

    # A set that keeps ANSWERING standby is refusing: fail early, name the
    # TV, and never reach Enter - the 47-121 s of learning it from the PC is
    # the cost this gate deletes.
    fresh_state()
    cglib.tv_power_state = lambda ip, timeout=2.0, raw=False: "standby"
    log, sent = wire([])                   # any ssh call would assert: no Enter
    assert couch.start() == 1
    ev = log.events()
    assert "launch_failed" in ev and "tv_on" not in ev, ev
    assert "TV never reported on" in cglib.LAST_ERROR.read_text()
    assert log.find("launch_start")[0]["tv"] == "standby"
    assert set(sent) == {"power_on"} and len(sent) >= 2, \
        f"the gate re-pokes while it waits, and never switches input: {sent}"
    assert not cglib.LOCK.exists()
    print("  tv gate: answered standby -> early honest failure, Enter never ran")

    # The healthy shape: standby while the frame lands, then on -> Enter.
    fresh_state()
    seq = ["standby", "standby", "standby"]          # first pop = launch_start read
    cglib.tv_power_state = \
        lambda ip, timeout=2.0, raw=False: (seq.pop(0) if seq else "on")
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_on" in ev and "host_ready" in ev, ev
    assert ev.index("tv_on") < ev.index("enter_dispatched"), \
        "Enter must not be dispatched before the set reports on"
    assert "hdmi4" in sent
    print("  tv gate: standby -> on -> Enter dispatched, launch lands")

    # A set that cannot be READ is not a refused one: stand down to the
    # legacy blind path rather than fail a launch that would have worked.
    fresh_state()
    cglib.tv_power_state = lambda ip, timeout=2.0, raw=False: None
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    ev = log.events()
    assert "tv_state_unknown" in ev and "host_ready" in ev, ev
    assert log.find("launch_start")[0]["tv"] is None
    print("  tv gate: unreadable set -> fail open, legacy launch unharmed")

    # No tvIp: the gate must not exist - not a read, not a field.
    fresh_state()

    def boom(ip, timeout=2.0, raw=False):
        raise AssertionError("no tvIp - the launch must never read the TV")
    cglib.tv_power_state = boom
    couch.CFG = CFG
    log, sent = wire([
        ("enter", "OK"),
        ("status", "ab12cd"),
        ("status", "NOTREADY"),
    ])
    assert couch.start(turn="ab12cd") == 0
    assert "tv" not in log.find("launch_start")[0], log.find("launch_start")
    cglib.tv_power_state = real_tv_state
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
    assert "reconcile_cleared" in log.events()
    assert not sent, "a dead session's reconcile must not drive the TV"
    assert not cglib.LOCK.exists()

    fresh_state(60)
    log, sent = wire([("status", RuntimeError("boot")),
                      ("status", RuntimeError("boot")),
                      ("status", RuntimeError("boot"))])
    assert couch.reconcile() == 0
    assert "reconcile_cleared" in log.events() and not cglib.LOCK.exists()

    fresh_state(None)
    log, sent = wire([])
    assert couch.reconcile() == 0 and not sent   # no lock: nothing to do
    print("  reconcile: resumes live, clears dead with TV untouched, "
          "shrugs off boot-time errors")

    time.sleep = real_sleep
    print("OK - couch: atomic acquire, ownership, one-rule ordering, failure "
          "release, watch death, reconcile paths")


if __name__ == "__main__":
    main()
