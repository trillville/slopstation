"""Blind test: couch.py's orchestration state machine, with every side effect
stubbed at its documented seam (ssh, exlink, wol, wait_port). What it pins:

  * the lock is taken ATOMICALLY - two racers, exactly one winner - and
    release refuses a successor's lock;
  * READY identity: our turn verifies, a foreign one is waited out, a
    timestamp is legacy-accepted, and a changed one supersedes the watcher;
  * the one rule - the TV input switch happens only after READY, never on a
    failure path, which also leaves last_error for the listener;
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

    def fake_exlink(name):
        sent.append(name)
        log("exlink_send", cmd=name)             # so event ORDER proves the one rule

    def fake_ssh(cmd, timeout=15):
        if script:
            verb, reply = script.pop(0)
            assert cmd.startswith(verb), f"expected {verb!r}, couch sent {cmd!r}"
        elif default is not None:
            reply = default(cmd)
        else:
            raise AssertionError(f"unscripted ssh call: {cmd!r}")
        if isinstance(reply, Exception):
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
            # (Windows hands the loser a sharing violation, not
            # FileExistsError - a crashed racer leaves None here.)
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
    # The one rule, as event order: the gaming input goes out only after READY.
    switches = [i for i, r in enumerate(log.records)
                if r["event"] == "exlink_send" and r["cmd"] == "hdmi4"]
    ready_at = ev.index("host_ready")
    assert switches and all(i > ready_at for i in switches), (ev, switches)
    assert sent == ["power_on", "hdmi4", "power_off"], sent
    assert "game_launch" in ev and "session_ended" in ev and "session_idle" in ev
    # A timestamp marker is the LEGACY shape (pre-turn-stamping PC): accepted
    # so either machine can deploy first, but flagged as unverified.
    assert log.find("host_ready")[0]["verified"] is False
    assert not cglib.LOCK.exists(), "session end must release the lock"
    assert not cglib.LAST_ERROR.exists(), "success must clear last_error"
    print("  start: input switch strictly after READY, appid queued, lock released")

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

    # --- watch: a successor's turn in the marker means stand down -------------
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
