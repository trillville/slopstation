"""Test TV volume ducking and restoration."""

import dataclasses

from helpers import CapturingLog
from slopstation import tv
from slopstation.agent.speech.ducking import TvDucker


@dataclasses.dataclass
class FakeRoom:
    """TV+bar as the ducker sees them: power state, volume reads and writes -
    with scriptable incomplete writes, readback death, and a hand on the remote. A
    scenario sets the knobs up front by keyword and turns them mid-session
    through set()."""

    power: str | None = "on"
    vol: int = 14
    drop: int = 0  # steps a write fails to move
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
        landed = max(0, n - self.drop)
        self.drop -= min(n, self.drop)
        if target < self.vol:
            self.vol -= min(landed, self.vol)  # floor 0
        else:
            self.vol += min(landed, 100 - self.vol)  # ceiling 100


def ducker(monkeypatch, steps=10, room=None, **kw):
    """A ducker on a FakeRoom (vol 14, on, by default); pause is a no-op so
    the readback polls do not wait."""
    room = room or FakeRoom()
    log = CapturingLog("voice")
    monkeypatch.setattr(tv, "tv_power_state", lambda ip, **kw: room.probe())
    monkeypatch.setattr(tv, "tv_volume", lambda ip: room.read())
    monkeypatch.setattr(tv, "tv_set_volume", lambda ip, level: room.write(level))
    monkeypatch.setattr(tv.time, "sleep", lambda s: None)
    dk = TvDucker(steps, tv.Tv({"tvIp": "192.0.2.1"}, log), log, **kw)
    return dk, room, log


def test_gate_a_set_that_is_not_on_is_not_touched(monkeypatch):
    dk, room, log = ducker(monkeypatch, room=FakeRoom(power="standby"))
    dk.duck()
    assert room.writes == [] and log.events() == ["tv_duck_skipped"], log.records
    dk.unduck()
    assert room.writes == [] and log.events() == ["tv_duck_skipped"]

    dk, room, log = ducker(
        monkeypatch, room=FakeRoom(power=None)
    )  # unreachable = unknown
    dk.duck()
    assert room.writes == [] and log.find("tv_duck_skipped")[0]["state"] == "unknown"


def test_no_readback_means_no_duck(monkeypatch):
    dk, room, log = ducker(monkeypatch, room=FakeRoom(readback_dead=True))
    dk.duck()
    assert (
        room.writes == [] and log.find("tv_duck_skipped")[0]["reason"] == "no_readback"
    )


def test_happy_pair_down_to_target_and_back_to_the_exact_start(monkeypatch):
    dk, room, log = ducker(monkeypatch, steps=10)  # vol 14
    dk.duck()
    assert room.vol == 4 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 10 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 14 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["steps"] == 10 and u0["ok"] is True, u0


def test_clamp_at_zero_ok_is_intent_achieved_and_the_delta_stays_honest(monkeypatch):
    dk, room, log = ducker(monkeypatch, steps=10, room=FakeRoom(vol=6))
    dk.duck()
    assert room.vol == 0 and dk.out == 6
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 6 and d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 6 and dk.out == 0


def test_incomplete_write_restores_only_verified_movement(monkeypatch):
    dk, room, log = ducker(monkeypatch, steps=10, room=FakeRoom(drop=3))
    dk.duck()
    assert room.vol == 7 and dk.out == 7
    assert room.writes == [4], room.writes
    assert log.find("tv_ducked")[0]["ok"] is False
    dk.unduck()
    assert room.vol == 14 and dk.out == 0


def test_write_failure_verifies_nothing_and_owes_nothing(monkeypatch):
    dk, room, log = ducker(
        monkeypatch, room=FakeRoom(write_error=RuntimeError("HTTP down"))
    )
    dk.duck()
    assert room.vol == 14 and dk.out == 0
    d0 = log.find("tv_ducked")[0]
    assert d0["steps"] == 0 and d0["ok"] is False, d0
    assert log.find("tv_duck_failed"), log.records
    dk.unduck()  # ledger empty: no-op
    assert [e for e in log.events() if e == "tv_unducked"] == []


def test_a_human_on_the_remote_mid_session_wins(monkeypatch):
    # Detected, not stomped.
    dk, room, log = ducker(monkeypatch, steps=10)
    dk.duck()
    room.set(vol=20)  # user turned it UP mid-game
    dk.unduck()
    assert room.vol == 20 and dk.out == 0
    u0 = log.find("tv_unducked")[0]
    assert u0["reason"] == "user_adjusted" and u0["steps"] == 0 and u0["ok"] is True, u0


def test_debt_when_readback_dies_at_close_a_later_close_restores_exactly(monkeypatch):
    dk, room, log = ducker(monkeypatch, steps=10)
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


def test_percentage_mode_scales_the_drop_with_the_pre_duck_level(monkeypatch):
    dk, room, log = ducker(monkeypatch, steps=0, room=FakeRoom(vol=20), to_pct=50)
    dk.duck()
    assert room.vol == 10 and dk.out == 10
    d0 = log.find("tv_ducked")[0]
    assert d0["asked"] == 10 and d0["ok"] is True, d0
    dk.unduck()
    assert room.vol == 20 and dk.out == 0

    dk, room, log = ducker(monkeypatch, steps=0, room=FakeRoom(vol=8), to_pct=50)
    dk.duck()  # same knob, quieter room
    assert room.vol == 4 and log.find("tv_ducked")[0]["asked"] == 4

    dk, room, log = ducker(monkeypatch, steps=3, room=FakeRoom(vol=20), to_pct=50)
    dk.duck()  # pct wins over steps
    assert room.vol == 10, room.vol


def test_a_close_that_could_not_reach_the_set_means_no_silence_and_no_swing(
    monkeypatch,
):
    # The TV went down before the restore, so the bar is still 15 low. The
    # next wake must neither duck again (that lands on 0) nor repay first
    # (the room would jump up and back down); the close settles it.
    dk, room, log = ducker(monkeypatch, steps=15, room=FakeRoom(vol=22))
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


def test_dry_run_balances_the_books_and_writes_nothing(monkeypatch):
    dk, room, log = ducker(monkeypatch, dry_run=True)
    dk.duck()
    dk.unduck()
    assert room.writes == [] and dk.out == 0
    assert [e for e in log.events() if e == "dry_run_would"] and not log.find(
        "tv_ducked"
    ), log.records
