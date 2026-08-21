"""Blind test: TvDucker - the on-gate, the landed-steps ledger, restore
retries, and the debt that makes a failed restore self-heal on the next
session's close. Every scenario is the 2026-08-16 incident replayed against
the fix. Run:
    .venv\\Scripts\\python tests\\test_ducking.py
"""
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cglib
import dispatch as dp

CFG = {"tvComPort": "COMX", "tvGamingCmd": "hdmi4",
       "voice": {"volumeStep": 3, "volumeMax": 40, "inputs": {}}}

DOWN = cglib.EXLINK_FRAMES["vol_down"]
UP = cglib.EXLINK_FRAMES["vol_up"]

attempts = []      # every frame written, acked or not
sent = []          # frames the fake TV acked
script = deque()   # per-frame behavior, consumed in order; empty = "ok"


def fake_exlink(frame, port):
    attempts.append(frame)
    if script and script.popleft() == "nak":
        raise cglib.ExlinkNak(f"TV answered nothing (want 030cf1) for frame {frame}")
    sent.append(frame)
    return "030cf1"


def reset():
    attempts.clear()
    sent.clear()
    script.clear()


def ducker(steps=4, probe="on"):
    """A TvDucker over a real Dispatch and a scripted TV. `probe` is a state
    string or a callable, so a test can flip the set mid-scenario."""
    log = cglib.CapturingLog("voice")
    d = dp.Dispatch(CFG, log)
    fn = probe if callable(probe) else (lambda: probe)
    return dp.TvDucker(d, steps, "192.0.2.1", probe=fn,
                       pause=lambda s: None), log


def tv_events(log):
    """The duck story alone - _exlink's own exlink_send/exlink_nak lines
    interleave with it and are drilled by test_dispatch, not here."""
    return [e for e in log.events() if e.startswith("tv_")]


def main():
    real_sleep = time.sleep
    time.sleep = lambda s: None                       # fast tests
    cglib.exlink_send_hex = fake_exlink

    # --- the gate: a set that is not on is not touched -----------------------
    reset()
    dk, log = ducker(probe="standby")
    dk.duck()
    assert attempts == [], attempts
    assert tv_events(log) == ["tv_duck_skipped"], log.records
    dk.unduck()                                       # nothing out, nothing owed
    assert attempts == [] and tv_events(log) == ["tv_duck_skipped"]

    reset()
    dk, log = ducker(probe=lambda: None)              # unreachable = unknown = skip
    dk.duck()
    assert attempts == [], attempts
    assert log.find("tv_duck_skipped")[0]["state"] == "unknown", log.records

    # --- the happy pair: N down, N up, ledger empty --------------------------
    reset()
    dk, log = ducker(steps=4)
    dk.duck()
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 4 and d0["asked"] == 4 and d0["ok"] is True, d0
    dk.unduck()
    assert sent == [DOWN] * 4 + [UP] * 4, sent
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 4 and u0["ok"] is True, u0
    assert dk.out == 0

    # --- duck aborts mid-burst: restore what LANDED, not what was asked ------
    reset()
    dk, log = ducker(steps=4)
    script.extend(["ok", "ok", "nak"])                # third vol_down dies
    dk.duck()
    assert sent == [DOWN] * 2, sent                   # and the burst stopped there
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 2 and d0["asked"] == 4 and d0["ok"] is False, d0
    dk.unduck()
    assert sent == [DOWN] * 2 + [UP] * 2, sent

    # --- a flaky restore step is retried and still restores fully ------------
    reset()
    dk, log = ducker(steps=3)
    dk.duck()
    script.extend(["nak", "ok"])                      # first vol_up misses once
    dk.unduck()
    assert sent == [DOWN] * 3 + [UP] * 3, sent
    assert len(attempts) == 3 + 4, attempts           # the retry frame is visible
    assert log.find("tv_unducked")[0]["ok"] is True and dk.out == 0

    # --- receiver gone (the 10:59:26 shape): bounded frames, debt kept, ------
    # --- and the NEXT session's close restores everything --------------------
    reset()
    dk, log = ducker(steps=4)
    dk.duck()
    script.extend(["nak"] * dp.TvDucker.TRIES)        # every vol_up dies
    dk.unduck()
    assert dk.out == 4
    assert attempts.count(UP) == dp.TvDucker.TRIES, attempts   # a handful, not a storm
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 0 and u0["asked"] == 4 and u0["ok"] is False, u0
    assert log.find("tv_duck_deficit")[0]["steps"] == 4, log.records
    dk.duck()                                         # TV answers again next wake
    assert dk.out == 8                                # new duck rides on the debt
    dk.unduck()
    assert dk.out == 0 and sent[-8:] == [UP] * 8, sent

    # --- debt heals even when the next session's duck is skipped -------------
    reset()
    state = {"now": "on"}
    dk, log = ducker(steps=2, probe=lambda: state["now"])
    dk.duck()
    script.extend(["nak"] * dp.TvDucker.TRIES)        # restore fails: debt 2
    dk.unduck()
    assert dk.out == 2
    state["now"] = "standby"                          # user shut the TV off
    dk.duck()                                         # next wake: gate skips
    assert log.find("tv_duck_skipped")[0]["debt"] == 2, log.records
    dk.unduck()                                       # close still pays the debt
    assert dk.out == 0 and sent[-2:] == [UP] * 2, sent

    time.sleep = real_sleep
    print("OK - ducking: on-gate (standby/unknown skip), landed-steps ledger, "
          "abort mid-burst, restore retry, bounded wedge frames, deficit debt, "
          "self-heal on next close (with and without a skipped duck)")


if __name__ == "__main__":
    main()
