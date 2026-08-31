"""Blind test: TvDucker - on-gate, verified ledger, key-loss top-ups, user
takeover, and the debt that self-heals a failed restore. Run:
    .venv\\Scripts\\python tests\\test_ducking.py
"""
import time

import _bootstrap  # noqa: F401

import cglib
from agent.tools import tv_remote


class FakeRoom:
    """TV+bar as the ducker sees them: power state, readback volume, keys -
    with scriptable key loss, readback death, and a hand on the remote."""

    def __init__(self, power="on", vol=14):
        self.power = power
        self.vol = vol
        self.readback_dead = False
        self.drop = 0                   # keys the CEC relay eats
        self.press_error = None
        self.presses = []               # every (direction, n) burst

    def probe(self):
        return self.power

    def read(self):
        return None if self.readback_dead else self.vol

    def press(self, direction, n):
        self.presses.append((direction, n))
        if self.press_error:
            raise self.press_error
        landed = max(0, n - self.drop)
        self.drop = max(0, self.drop - n)
        self.vol = (max(0, self.vol - landed) if direction == "down"
                    else min(100, self.vol + landed))


def ducker(steps=10, room=None, **kw):
    room = room or FakeRoom()
    log = cglib.CapturingLog("voice")
    dk = tv_remote.TvDucker(steps, "192.0.2.1", log,
                     probe=room.probe, read=room.read, press=room.press,
                     pause=lambda s: None, **kw)
    return dk, room, log


def main():
    real_sleep = time.sleep
    time.sleep = lambda s: None                       # fast tests

    # --- gate: a set that is not on is not touched ---------------------------
    dk, room, log = ducker(room=FakeRoom(power="standby"))
    dk.duck()
    assert room.presses == [] and log.events() == ["tv_duck_skipped"], log.records
    dk.unduck()
    assert room.presses == [] and log.events() == ["tv_duck_skipped"]

    dk, room, log = ducker(room=FakeRoom(power=None))  # unreachable = unknown
    dk.duck()
    assert room.presses == [] and log.find("tv_duck_skipped")[0]["state"] == "unknown"

    # --- no readback = no duck -----------------------------------------------
    room = FakeRoom()
    room.readback_dead = True
    dk, room, log = ducker(room=room)
    dk.duck()
    assert room.presses == [] and log.find("tv_duck_skipped")[0]["reason"] == "no_readback"

    # --- happy pair: down to target, back to the exact start -----------------
    dk, room, log = ducker(steps=10)                  # vol 14
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 10 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 14 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 10 and u0["ok"] is True, u0

    # --- clamp at zero: ok = intent achieved, delta stays honest -------------
    dk, room, log = ducker(steps=10, room=FakeRoom(vol=6))
    dk.duck()
    assert room.vol == 0 and dk.out == 6
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 6 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 6 and dk.out == 0

    # --- lost keys: readback notices, a top-up round finishes ----------------
    room = FakeRoom()
    room.drop = 3                                     # relay eats 3 of the burst
    dk, room, log = ducker(steps=10, room=room)
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    assert room.presses == [("down", 10), ("down", 3)], room.presses
    assert log.find("tv_ducked")[0]["ok"] is True

    # --- relay dead: nothing verified, nothing owed (08-21 ack-then-refuse) --
    room = FakeRoom()
    room.press_error = RuntimeError("ws down")
    dk, room, log = ducker(room=room)
    dk.duck()
    assert room.vol == 14 and dk.out == 0
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False, d0
    assert log.find("tv_duck_failed"), log.records
    dk.unduck()                                       # ledger empty: no-op
    assert [e for e in log.events() if e == "tv_unducked"] == []

    # --- a human on the remote mid-session wins: detected, not stomped -------
    dk, room, log = ducker(steps=10)
    dk.duck()
    room.vol = 20                                     # user turned it UP mid-game
    dk.unduck()
    assert room.vol == 20 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["reason"] == "user_adjusted" and u0["steps"] == 0 and u0["ok"] is True, u0

    # --- debt: readback dies at close, a LATER close restores exactly --------
    dk, room, log = ducker(steps=10)
    dk.duck()
    room.readback_dead = True
    dk.unduck()                                       # cannot verify: keep debt
    assert dk.out == 10
    assert log.find("tv_unducked")[0]["reason"] == "no_readback"
    assert log.find("tv_duck_deficit")[0]["steps"] == 10
    room.readback_dead = False
    dk.duck()                     # already down 10: left alone, debt unchanged
    assert dk.out == 10 and room.vol == 4, (dk.out, room.vol)
    assert log.find("tv_duck_skipped")[-1]["reason"] == "already_ducked"
    dk.unduck()
    assert room.vol == 14 and dk.out == 0, (room.vol, dk.out)

    # --- percentage mode: the drop scales with the pre-duck level ------------
    dk, room, log = ducker(steps=0, room=FakeRoom(vol=20), to_pct=50)
    dk.duck()
    assert room.vol == 10 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 20 and dk.out == 0

    dk, room, log = ducker(steps=0, room=FakeRoom(vol=8), to_pct=50)
    dk.duck()                                         # same knob, quieter room
    assert room.vol == 4 and log.find("tv_ducked")[0]["asked"] == 4

    dk, room, log = ducker(steps=3, room=FakeRoom(vol=20), to_pct=50)
    dk.duck()                                         # pct wins over steps
    assert room.vol == 10, room.vol

    # --- a close that could not reach the set: no silence, no swing ----------
    # The TV went down before the restore, so the bar is still 15 low. The
    # next wake must neither duck again (that lands on 0) nor repay first
    # (the room would jump up and back down); the close settles it.
    dk, room, log = ducker(steps=15, room=FakeRoom(vol=22))
    dk.duck()
    assert room.vol == 7 and dk.out == 15
    room.drop = 99                    # set going down: keys relay nowhere
    dk.unduck()
    assert dk.out == 15 and log.find("tv_duck_deficit"), log.records
    assert room.vol == 7, "the restore moved nothing"
    room.drop = 0                                     # next session, bar still low
    presses = len(room.presses)
    dk.duck()
    assert dk.out == 15 and room.vol == 7, (dk.out, room.vol)
    assert len(room.presses) == presses, "a bar already ducked is not touched"
    dk.unduck()                                       # the close pays it all back
    assert room.vol == 22 and dk.out == 0, (room.vol, dk.out)

    # --- dry run: books balance, nothing is pressed --------------------------
    dk, room, log = ducker(dry_run=True)
    dk.duck()
    dk.unduck()
    assert room.presses == [] and dk.out == 0
    assert [e for e in log.events() if e == "dry_run_would"] and \
        not log.find("tv_ducked"), log.records

    time.sleep = real_sleep
    print("OK - ducking: on-gate (standby/unknown/no-readback skip), verified "
          "ledger, clamp honesty, key-loss top-up, dead-relay zero-debt, "
          "user-takeover stand-down, deficit debt + exact self-heal, "
          "an owed duck left alone at the next wake, percentage mode (scales, wins "
          "over steps), dry run")


if __name__ == "__main__":
    main()
