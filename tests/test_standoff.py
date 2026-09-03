"""The chord listener's hands-off rule. The listener must hold no
handles on the Puck while a session owns it - VirtualHere hands the device to
the gaming PC, and a listener still reading its interfaces leaves an
enumerated-but-dead controller. A stale lock must still read as free, or the
chord lane goes deaf until someone deletes a file.
"""

import os
import time
from dataclasses import dataclass, field

import pytest

from helpers import CapturingLog, seed_lock
from slopstation import chord_listener as cl
from slopstation import events, logbook, sessionlock


@pytest.fixture(autouse=True)
def _no_startup_side_effects(monkeypatch):
    """main()'s two startup side effects belong to running the lane, not to
    exercising its loop. A fixture, not module scope: at module scope this ran
    during collection and stubbed the real start_heartbeat out from under
    every other test in the session."""
    monkeypatch.setattr(logbook, "rotate", lambda *a, **k: None)
    monkeypatch.setattr(events, "start_heartbeat", lambda *a, **k: None)


class FakeHandle:
    """A hid handle whose only behaviour is counting its close() calls."""

    closes = 0

    def close(self):
        self.closes += 1

    @property
    def closed(self):
        return self.closes > 0


class FakeDevice(FakeHandle):
    """What hid.device() hands the loop: opens, goes non-blocking, and never
    has an input report to give."""

    def open_path(self, path):
        pass

    def set_nonblocking(self, v):
        pass

    def read(self, n):
        return []


def held_puck():
    p = cl.Puck()
    h = FakeHandle()
    p.handles, p.active = [h], h
    return p, h


# --- driving the real loop ----------------------------------------------------
# The loop runs for real against a fake hid module and a fake sleep that counts
# iterations, starts a session part-way through, and breaks out.
class Stop(Exception):
    """Not OSError/ValueError - the loop's own handler must not swallow it."""


@dataclass
class FakeHid:
    """Stands in for the hid module, recording every enumerate() so a test can
    assert the listener never LOOKED at the device."""

    interfaces: int = 3
    enumerations: int = 0
    opened: list = field(default_factory=list)

    def enumerate(self, vid, pid):
        self.enumerations += 1
        return [{"path": f"p{i}".encode()} for i in range(self.interfaces)]

    def device(self):
        h = FakeDevice()
        self.opened.append(h)
        return h


@pytest.fixture
def drive(monkeypatch):
    """drive(lock_age_s, stop_after, session_starts_at=None, error_age_s=None)
    runs main()'s loop, counting sleeps. Returns (captured log, fake hid).
    lock_age_s seeds the lock; session_starts_at makes one appear mid-loop, so
    the standoff transition is exercised, not just the steady states.
    error_age_s seeds a failure marker of that age - FakeHid never returns a
    report, so this is the Puck sitting untouched."""

    def _drive(lock_age_s, stop_after, session_starts_at=None, error_age_s=None):
        seed_lock(lock_age_s)
        if error_age_s is not None:
            sessionlock.last_error_file().write_text("Enter exited without READY")
            when = time.time() - error_age_s
            os.utime(sessionlock.last_error_file(), (when, when))
        cap = CapturingLog("listener")
        fake = FakeHid()
        ticks = [0]

        def fake_sleep(_s):
            ticks[0] += 1
            if session_starts_at is not None and ticks[0] == session_starts_at:
                seed_lock(0)  # a launch just took the lock
            if ticks[0] >= stop_after:
                raise Stop()

        monkeypatch.setattr(cl, "log", cap)
        monkeypatch.setattr(cl, "hid", fake)
        monkeypatch.setattr(time, "sleep", fake_sleep)
        # Check the lock every pass: fake_sleep advances no real clock, and this
        # is about the transition, not the poll rate.
        monkeypatch.setattr(cl, "STANDOFF_POLL_S", 0)
        monkeypatch.setattr(cl, "FAIL_CHECK_S", 0)
        with pytest.raises(Stop):
            cl.main()
        return cap, fake

    return _drive


# --- session_active: the arbiter the standoff rides on ------------------------


def test_active_reads_a_fresh_lock_as_spoken_for_and_a_stale_one_as_free():
    seed_lock(None)
    assert sessionlock.active() is False, "no lock must read as free"

    seed_lock(10)
    assert sessionlock.active() is True, "a fresh lock means the Puck is spoken for"

    seed_lock(sessionlock.LOCK_STALE_S - 5)
    assert sessionlock.active() is True, "just inside the window is still active"

    # A lock nobody cleaned up must not permanently deafen the chord lane.
    seed_lock(sessionlock.LOCK_STALE_S + 5)
    assert sessionlock.active() is False, "a stale lock must read as free"


# --- stand_off: lets go once, reports only the transition ---------------------


def test_stand_off_lets_go_once_and_reports_only_the_transition():
    puck, h = held_puck()
    assert puck.stand_off() is True, "first call must let go"
    assert h.closed, "the handle was not actually closed"
    assert puck.handles == [] and puck.active is None, "state not cleared"
    assert puck.stand_off() is False, "already off the device - must not re-log"

    # A Puck that never held anything is already standing off.
    assert cl.Puck().stand_off() is False


# --- the real loop, wired as it ships -----------------------------------------


def test_a_session_that_owns_the_puck_is_never_enumerated(drive):
    # A session already owns the Puck: the listener must not enumerate HID.
    cap, fake = drive(lock_age_s=10, stop_after=6)
    assert fake.enumerations == 0, "stood off but still went looking for the Puck"
    assert "puck_present" not in cap.events(), cap.events()
    assert "puck_standoff" not in cap.events(), (
        "held nothing - a transition must not be logged, or a session spams it"
    )


def test_an_idle_listener_opens_and_listens(drive):
    # Idle: no lock, so it opens and listens like always.
    cap, fake = drive(lock_age_s=None, stop_after=6)
    assert fake.enumerations >= 1, "idle listener never opened the Puck"
    assert cap.events()[0] == "puck_present", cap.events()


def test_a_stale_lock_is_not_a_session(drive):
    # A stale lock is NOT a session - the listener must come back.
    cap, fake = drive(lock_age_s=sessionlock.LOCK_STALE_S + 5, stop_after=6)
    assert fake.enumerations >= 1, "a stale lock left the chord lane deaf"


def test_the_transition_stands_off_once_and_never_reopens(drive):
    # THE transition: listening, then a launch takes the lock mid-loop.
    cap, fake = drive(lock_age_s=None, stop_after=14, session_starts_at=4)
    ev = cap.events()
    assert ev[0] == "puck_present" and "puck_standoff" in ev, ev
    assert ev.index("puck_standoff") > ev.index("puck_present"), ev
    assert all(h.closed for h in fake.opened), "handles left open through the claim"
    assert fake.enumerations == 1, (
        f"re-opened the Puck {fake.enumerations - 1}x mid-session - the standoff leaks"
    )
    assert ev.count("puck_standoff") == 1, (
        f"logged the transition {ev.count('puck_standoff')}x"
    )


def test_the_loop_ages_out_a_stale_error_marker_while_the_puck_says_nothing(drive):
    # The loop itself must reach the age-out while the Puck says nothing:
    # FakeHid never returns a report, which is the state a voice- or
    # phone-started launch fails in.
    cap, _ = drive(lock_age_s=None, stop_after=6, error_age_s=cl.ERR_STALE_S + 60)
    assert "stale_error_discarded" in cap.events(), cap.events()
    assert not sessionlock.last_error_file().exists(), (
        "the loop never aged the marker out"
    )


def test_a_fresh_error_marker_is_kept_unheld(monkeypatch):
    # Fresh and unheld: kept, so the next hand on the Puck still feels it.
    cap = CapturingLog("listener")
    monkeypatch.setattr(cl, "log", cap)
    sessionlock.last_error_file().write_text("Enter exited without READY")
    cl.signal_last_error(None)
    assert not cap.events(), cap.events()
    assert sessionlock.last_error_file().exists(), "fresh marker discarded unheld"
