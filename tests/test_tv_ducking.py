"""Test TV volume ducking and restoration."""

import dataclasses

from helpers import CapturingLog
from slopstation.agent.tools import tv_remote


@dataclasses.dataclass
class FakeRoom:
    """TV+bar as the ducker sees them: power state, volume reads and writes -
    with scriptable short writes (drop), writes the set accepts and ignores
    (ignore), a raising write, readback death, and a hand on the remote. A
    scenario sets the knobs up front by keyword and turns them mid-session
    through set()."""

    power: str | None = "on"
    vol: int = 14
    drop: int = 0  # steps a write fails to move
    ignore: int = 0  # writes the set accepts (HTTP 200) and does not apply
    readback_dead: bool = False
    write_error: Exception | None = None
    writes: list = dataclasses.field(default_factory=list)  # requested absolute levels

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

    def write(self, target):
        self.writes.append(target)
        n = abs(target - self.vol)
        if self.write_error:
            raise self.write_error
        if self.ignore:
            self.ignore -= 1
            return
        landed = max(0, n - self.drop)
        self.drop -= min(n, self.drop)
        if target < self.vol:
            self.vol -= min(landed, self.vol)  # floor 0
        else:
            self.vol += min(landed, 100 - self.vol)  # ceiling 100


def ducker(steps=10, room=None, **kw):
    """A ducker on a FakeRoom (vol 14, on, by default); pause is a no-op so
    the readback polls do not wait."""
    room = room or FakeRoom()
    log = CapturingLog("voice")
    dk = tv_remote.TvDucker(
        steps,
        "192.0.2.1",
        log,
        probe=room.probe,
        read=room.read,
        write=room.write,
        pause=lambda s: None,
        clock=kw.pop("clock", lambda: 0.0),
        **kw,
    )
    return dk, room, log


def test_a_hung_readback_cannot_hold_the_move_past_its_budget():
    # Each read answers 14 (unmoved) after a full HTTP timeout: the deadline
    # ends the move after the 2.4 s budget, not after 24 x the timeout.
    now = [0.0]
    reads = [0]

    def slow_read():
        now[0] += tv_remote.TvVolume.HTTP_TIMEOUT_S
        reads[0] += 1
        return 14

    dk, room, log = ducker(steps=10, room=FakeRoom(ignore=99), clock=lambda: now[0])
    dk.read = slow_read
    dk.duck()
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False, d0
    budget = tv_remote.TvVolume.POLLS * tv_remote.TvVolume.POLL_GAP_S
    assert reads[0] <= budget / tv_remote.TvVolume.HTTP_TIMEOUT_S + 2, reads
    assert now[0] <= budget + 2 * tv_remote.TvVolume.HTTP_TIMEOUT_S, now


def test_the_retry_waits_half_a_second_and_the_budget_is_24_polls():
    pauses = []
    room = FakeRoom(ignore=1)
    dk, room, log = ducker(steps=10, room=room)
    dk.pause = pauses.append
    dk.duck()
    # RETRY_AFTER polls after the first write, the second lands on its first poll.
    assert len(pauses) == tv_remote.TvVolume.RETRY_AFTER - 1, pauses
    dk, room, log = ducker(steps=10, room=FakeRoom(ignore=99))
    pauses.clear()
    dk.pause = pauses.append
    dk.duck()
    assert len(pauses) == tv_remote.TvVolume.POLLS - 3, "three settles, 24 polls"


def test_gate_a_set_that_is_not_on_is_not_touched():
    dk, room, log = ducker(room=FakeRoom(power="standby"))
    assert dk.duck() is None, "a set that is off leaves the room quiet"
    assert room.writes == [] and log.events() == ["tv_duck_skipped"], log.records
    dk.unduck()
    assert room.writes == [] and log.events() == ["tv_duck_skipped"]

    dk, room, log = ducker(room=FakeRoom(power=None))  # unreachable = unknown
    dk.duck()
    assert room.writes == [] and log.find("tv_duck_skipped")[0]["state"] == "unknown"


def test_no_readback_means_no_duck():
    dk, room, log = ducker(room=FakeRoom(readback_dead=True))
    assert dk.duck() is None, "no readback says nothing about the room"
    assert (
        room.writes == [] and log.find("tv_duck_skipped")[0]["reason"] == "no_readback"
    )


def test_readback_dying_after_the_write_stops_the_retries():
    # Nothing more can be verified, so the remaining writes are not sent.
    dk, room, log = ducker(steps=10, room=FakeRoom(ignore=99))
    dk.read = lambda: None if room.writes else room.vol  # dies after the write
    dk.duck()
    assert room.writes == [4] and dk.out == 0, room.writes
    assert log.find("tv_ducked")[0]["writes"] == 1


def test_happy_pair_down_to_target_and_back_to_the_exact_start():
    dk, room, log = ducker(steps=10)  # vol 14
    assert dk.duck() is True
    assert room.vol == 4 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 10 and d0["asked"] == 10 and d0["ok"] is True, d0
    assert d0["writes"] == 1, "the good path is one write"
    dk.unduck()
    assert room.vol == 14 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 10 and u0["ok"] is True and u0["writes"] == 1, u0


def test_clamp_at_zero_ok_is_intent_achieved_and_the_delta_stays_honest():
    dk, room, log = ducker(steps=10, room=FakeRoom(vol=6))
    dk.duck()
    assert room.vol == 0 and dk.out == 6
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 6 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 6 and dk.out == 0


def test_a_write_that_moved_but_stopped_short_is_left_alone():
    # A level that moved is verified movement, and possibly a hand on the
    # remote: no second absolute write over it.
    dk, room, log = ducker(steps=10, room=FakeRoom(drop=3))
    dk.duck()
    assert room.vol == 7 and dk.out == 7
    assert room.writes == [4], room.writes
    d0 = log.find("tv_ducked")[0]
    assert d0["ok"] is False and d0["writes"] == 1, d0
    dk.unduck()
    assert room.vol == 14 and dk.out == 0


def test_a_hand_on_the_remote_during_the_verify_is_not_overwritten():
    # Restore 4->14 ignored by the set; the person turns it to 8 meanwhile.
    # The retry must not write 14 over their 8.
    dk, room, log = ducker(steps=10)
    dk.duck()
    assert room.vol == 4
    room.set(ignore=1)
    polls = [0]

    def read():
        polls[0] += 1
        if polls[0] == 2:
            room.set(vol=8)
        return room.vol

    dk.read = read
    dk.unduck()
    assert room.vol == 8, "their level survived"
    assert room.writes == [4, 14], room.writes


def test_a_write_the_set_accepted_but_ignored_is_sent_again():
    # 2026-09-05: SetVolume answered 200, the bar stayed at 30, and the next
    # session's identical write took at once. One retry covers it.
    dk, room, log = ducker(steps=10, room=FakeRoom(ignore=1))
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    assert room.writes == [4, 4], room.writes
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 10 and d0["ok"] is True and d0["writes"] == 2, d0
    assert not log.find("tv_duck_failed"), "an accepted write is not a failure"


def test_a_set_that_ignores_every_write_gets_three_not_a_storm():
    dk, room, log = ducker(steps=10, room=FakeRoom(ignore=99))
    assert dk.duck() is False, "a duck that did not land leaves the room loud"
    assert room.vol == 14 and dk.out == 0
    assert room.writes == [4, 4, 4], room.writes
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False and d0["writes"] == 3, d0
    dk.unduck()  # ledger empty: no-op
    assert [e for e in log.events() if e == "tv_unducked"] == []


def test_write_failure_verifies_nothing_and_owes_nothing():
    dk, room, log = ducker(room=FakeRoom(write_error=RuntimeError("HTTP down")))
    dk.duck()
    assert room.vol == 14 and dk.out == 0
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False and d0["writes"] == 3, d0
    assert len(log.find("tv_duck_failed")) == 3, log.records
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
    assert dk.duck() is True  # already down 10: left alone, debt unchanged
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
    room.set(drop=99)  # set going down: writes have no effect
    dk.unduck()
    assert dk.out == 15 and log.find("tv_duck_deficit"), log.records
    assert room.vol == 7, "the restore moved nothing"
    room.set(drop=0)  # next session, bar still low
    writes = len(room.writes)
    dk.duck()
    assert dk.out == 15 and room.vol == 7, (dk.out, room.vol)
    assert len(room.writes) == writes, "a bar already ducked is not touched"
    dk.unduck()  # the close pays it all back
    assert room.vol == 22 and dk.out == 0, (room.vol, dk.out)


def test_dry_run_balances_the_books_and_writes_nothing():
    dk, room, log = ducker(dry_run=True)
    dk.duck()
    dk.unduck()
    assert room.writes == [] and dk.out == 0
    assert [e for e in log.events() if e == "dry_run_would"] and not log.find(
        "tv_ducked"
    ), log.records
