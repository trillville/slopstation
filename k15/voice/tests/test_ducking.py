"""Blind test: TvDucker - the on-gate, the readback-verified ledger, key-loss
top-ups, user-takeover detection, and the debt that makes a failed restore
self-heal on a later close. The scenarios replay both incidents (08-16 blind
bursts, 08-21 eARC ack-then-refuse) against the fix. Run:
    .venv\\Scripts\\python tests\\test_ducking.py
"""
import _bootstrap  # noqa: F401

from _bootstrap import freeze_sleep

import cglib
import ducking


class FakeRoom:
    """The TV+bar as the ducker sees them: a power state, the volume the
    readback reports, and keys that move it - with scriptable loss, death,
    and a human hand on the remote."""

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
    dk = ducking.TvDucker(steps, "192.0.2.1", log,
                     probe=room.probe, read=room.read, press=room.press,
                     pause=lambda s: None, **kw)
    return dk, room, log


def main():
    # --- the gate: a set that is not on is not touched -----------------------
    dk, room, log = ducker(room=FakeRoom(power="standby"))
    dk.duck()
    assert room.presses == [] and log.events() == ["tv_duck_skipped"], log.records
    dk.unduck()                                       # nothing out, nothing owed
    assert room.presses == [] and log.events() == ["tv_duck_skipped"]

    dk, room, log = ducker(room=FakeRoom(power=None))  # unreachable = unknown
    dk.duck()
    assert room.presses == [] and log.find("tv_duck_skipped")[0]["state"] == "unknown"

    # --- no readback = no duck: never move what cannot be verified -----------
    room = FakeRoom()
    room.readback_dead = True
    dk, room, log = ducker(room=room)
    dk.duck()
    assert room.presses == [] and log.find("tv_duck_skipped")[0]["reason"] == "no_readback"

    # --- the happy pair: down to target, back to the exact start -------------
    dk, room, log = ducker(steps=10)                  # vol 14
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 10 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 14 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 10 and u0["ok"] is True, u0

    # --- clamp at zero: intent achieved counts as ok, delta stays honest -----
    dk, room, log = ducker(steps=10, room=FakeRoom(vol=6))
    dk.duck()
    assert room.vol == 0 and dk.out == 6
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 6 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 6 and dk.out == 0

    # --- lost keys: the readback notices, a top-up round finishes the job ----
    room = FakeRoom()
    room.drop = 3                                     # relay eats 3 of the burst
    dk, room, log = ducker(steps=10, room=room)
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    assert room.presses == [("down", 10), ("down", 3)], room.presses
    assert log.find("tv_ducked")[0]["ok"] is True

    # --- relay dead: nothing verified means nothing owed - the 08-21 ---------
    # --- ack-then-refuse shape can no longer inflate the ledger --------------
    room = FakeRoom()
    room.press_error = RuntimeError("ws down")
    dk, room, log = ducker(room=room)
    dk.duck()
    assert room.vol == 14 and dk.out == 0
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False, d0
    assert log.find("tv_duck_failed"), log.records    # the press failure traced
    dk.unduck()                                       # ledger empty: no-op
    assert [e for e in log.events() if e == "tv_unducked"] == []

    # --- a human on the remote mid-session wins: detected, not stomped -------
    dk, room, log = ducker(steps=10)
    dk.duck()                                         # 14 -> 4
    room.vol = 20                                     # user turned it UP mid-game
    dk.unduck()
    assert room.vol == 20 and dk.out == 0             # their choice stands
    u0 = log.find("tv_unducked")[0]
    assert u0["reason"] == "user_adjusted" and u0["steps"] == 0 and u0["ok"] is True, u0

    # --- debt: readback dies at close, and a LATER close restores the --------
    # --- original level exactly ----------------------------------------------
    dk, room, log = ducker(steps=10)
    dk.duck()                                         # 14 -> 4, out 10
    room.readback_dead = True
    dk.unduck()                                       # cannot verify: keep debt
    assert dk.out == 10
    assert log.find("tv_unducked")[0]["reason"] == "no_readback"
    assert log.find("tv_duck_deficit")[0]["steps"] == 10
    room.readback_dead = False                        # TV back; next session:
    dk.duck()                                         # 4 -> 0 (clamp), out 14
    assert dk.out == 14
    dk.unduck()                                       # one close pays it all off
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

    # --- dry run: books balance, nothing is pressed --------------------------
    dk, room, log = ducker(dry_run=True)
    dk.duck()
    dk.unduck()
    assert room.presses == [] and dk.out == 0
    assert [e for e in log.events() if e == "dry_run_would"] and \
        not log.find("tv_ducked"), log.records

    print("OK - ducking: on-gate (standby/unknown/no-readback skip), verified "
          "ledger, clamp honesty, key-loss top-up, dead-relay zero-debt, "
          "user-takeover stand-down, deficit debt + exact self-heal, "
          "percentage mode (scales, wins over steps), dry run")


if __name__ == "__main__":
    with freeze_sleep():
        main()
