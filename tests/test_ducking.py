"""TvDucker - on-gate, verified ledger, key-loss top-ups, user
takeover, and the debt that self-heals a failed restore.
"""

import dataclasses

from slopstation import logbook
from slopstation.agent.tools import tv_remote


@dataclasses.dataclass
class FakeRoom:
    """TV+bar as the ducker sees them: power state, readback volume, keys -
    with scriptable key loss, readback death, and a hand on the remote. A
    scenario sets the knobs up front by keyword and turns them mid-session
    through set()."""

    power: str | None = "on"
    vol: int = 14
    drop: int = 0  # keys the CEC relay eats
    readback_dead: bool = False
    press_error: Exception | None = None
    presses: list = dataclasses.field(default_factory=list)  # every (direction, n)

    def set(self, **knobs):
        """What the room does between the ducker's calls: a hand on the
        remote, the readback dying, the relay eating keys."""
        for knob, value in knobs.items():
            assert hasattr(self, knob), knob
            setattr(self, knob, value)

    def probe(self):
        return self.power

    def read(self):
        return None if self.readback_dead else self.vol

    def press(self, direction, n):
        self.presses.append((direction, n))
        if self.press_error:
            raise self.press_error
        landed = max(0, n - self.drop)
        self.drop -= min(n, self.drop)
        if direction == "down":
            self.vol -= min(landed, self.vol)  # floor 0
        else:
            self.vol += min(landed, 100 - self.vol)  # ceiling 100


def ducker(steps=10, room=None, **kw):
    """A ducker on a FakeRoom (vol 14, on, by default); pause is a no-op so
    the readback polls do not wait."""
    room = room or FakeRoom()
    log = logbook.CapturingLog("voice")
    dk = tv_remote.TvDucker(
        steps,
        "192.0.2.1",
        log,
        probe=room.probe,
        read=room.read,
        press=room.press,
        pause=lambda s: None,
        **kw,
    )
    return dk, room, log


def test_gate_a_set_that_is_not_on_is_not_touched():
    dk, room, log = ducker(room=FakeRoom(power="standby"))
    dk.duck()
    assert room.presses == [] and log.events() == ["tv_duck_skipped"], log.records
    dk.unduck()
    assert room.presses == [] and log.events() == ["tv_duck_skipped"]

    dk, room, log = ducker(room=FakeRoom(power=None))  # unreachable = unknown
    dk.duck()
    assert room.presses == [] and log.find("tv_duck_skipped")[0]["state"] == "unknown"


def test_no_readback_means_no_duck():
    dk, room, log = ducker(room=FakeRoom(readback_dead=True))
    dk.duck()
    assert (
        room.presses == [] and log.find("tv_duck_skipped")[0]["reason"] == "no_readback"
    )


def test_happy_pair_down_to_target_and_back_to_the_exact_start():
    dk, room, log = ducker(steps=10)  # vol 14
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 10 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 14 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 10 and u0["ok"] is True, u0


def test_clamp_at_zero_ok_is_intent_achieved_and_the_delta_stays_honest():
    dk, room, log = ducker(steps=10, room=FakeRoom(vol=6))
    dk.duck()
    assert room.vol == 0 and dk.out == 6
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 6 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 6 and dk.out == 0


def test_lost_keys_are_noticed_by_readback_and_a_top_up_round_finishes():
    dk, room, log = ducker(steps=10, room=FakeRoom(drop=3))  # relay eats 3
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    assert room.presses == [("down", 10), ("down", 3)], room.presses
    assert log.find("tv_ducked")[0]["ok"] is True


def test_relay_dead_verifies_nothing_and_owes_nothing():
    # 08-21 ack-then-refuse
    dk, room, log = ducker(room=FakeRoom(press_error=RuntimeError("ws down")))
    dk.duck()
    assert room.vol == 14 and dk.out == 0
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False, d0
    assert log.find("tv_duck_failed"), log.records
    dk.unduck()  # ledger empty: no-op
    assert [e for e in log.events() if e == "tv_unducked"] == []


def test_a_human_on_the_remote_mid_session_wins():
    # Detected, not stomped.
    dk, room, log = ducker(steps=10)
    dk.duck()
    room.set(vol=20)  # user turned it UP mid-game
    dk.unduck()
    assert room.vol == 20 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["reason"] == "user_adjusted" and u0["steps"] == 0 and u0["ok"] is True, u0


def test_debt_when_readback_dies_at_close_a_later_close_restores_exactly():
    dk, room, log = ducker(steps=10)
    dk.duck()
    room.set(readback_dead=True)
    dk.unduck()  # cannot verify: keep debt
    assert dk.out == 10
    assert log.find("tv_unducked")[0]["reason"] == "no_readback"
    assert log.find("tv_duck_deficit")[0]["steps"] == 10
    room.set(readback_dead=False)
    dk.duck()  # already down 10: left alone, debt unchanged
    assert dk.out == 10 and room.vol == 4, (dk.out, room.vol)
    assert log.find("tv_duck_skipped")[-1]["reason"] == "already_ducked"
    dk.unduck()
    assert room.vol == 14 and dk.out == 0, (room.vol, dk.out)


def test_percentage_mode_scales_the_drop_with_the_pre_duck_level():
    dk, room, log = ducker(steps=0, room=FakeRoom(vol=20), to_pct=50)
    dk.duck()
    assert room.vol == 10 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 20 and dk.out == 0

    dk, room, log = ducker(steps=0, room=FakeRoom(vol=8), to_pct=50)
    dk.duck()  # same knob, quieter room
    assert room.vol == 4 and log.find("tv_ducked")[0]["asked"] == 4

    dk, room, log = ducker(steps=3, room=FakeRoom(vol=20), to_pct=50)
    dk.duck()  # pct wins over steps
    assert room.vol == 10, room.vol


def test_a_close_that_could_not_reach_the_set_means_no_silence_and_no_swing():
    # The TV went down before the restore, so the bar is still 15 low. The
    # next wake must neither duck again (that lands on 0) nor repay first
    # (the room would jump up and back down); the close settles it.
    dk, room, log = ducker(steps=15, room=FakeRoom(vol=22))
    dk.duck()
    assert room.vol == 7 and dk.out == 15
    room.set(drop=99)  # set going down: keys relay nowhere
    dk.unduck()
    assert dk.out == 15 and log.find("tv_duck_deficit"), log.records
    assert room.vol == 7, "the restore moved nothing"
    room.set(drop=0)  # next session, bar still low
    presses = len(room.presses)
    dk.duck()
    assert dk.out == 15 and room.vol == 7, (dk.out, room.vol)
    assert len(room.presses) == presses, "a bar already ducked is not touched"
    dk.unduck()  # the close pays it all back
    assert room.vol == 22 and dk.out == 0, (room.vol, dk.out)


def test_dry_run_balances_the_books_and_presses_nothing():
    dk, room, log = ducker(dry_run=True)
    dk.duck()
    dk.unduck()
    assert room.presses == [] and dk.out == 0
    assert [e for e in log.events() if e == "dry_run_would"] and not log.find(
        "tv_ducked"
    ), log.records
