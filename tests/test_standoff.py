"""Blind test: the chord listener's hands-off rule. The listener must hold no
handles on the Puck while a session owns it - VirtualHere hands the device to
the gaming PC, and a listener still reading its interfaces leaves an
enumerated-but-dead controller. A stale lock must still read as free, or the
chord lane goes deaf until someone deletes a file.

Run:  python tests\\test_standoff.py     (system python - no venv needed)
"""
import os
import sys
import time
import types

import pytest

from helpers import fresh_state

# The listener runs on SYSTEM python and `hid` is not in the voice venv, so
# stub it: nothing here tests HID, only the decision to let go of it.
sys.modules.setdefault("hid", types.SimpleNamespace(
    enumerate=lambda *a, **k: [], device=object))

from slopstation import cglib
from slopstation import events
from slopstation import chord_listener as cl

_real_sleep = time.sleep
_real_poll = cl.STANDOFF_POLL_S
_real_fail_check = cl.FAIL_CHECK_S

@pytest.fixture(autouse=True)
def _no_startup_side_effects():
    """main()'s two startup side effects belong to running the lane, not to
    exercising its loop. A fixture, not module scope: at module scope this ran
    during collection and stubbed the real start_heartbeat out from under
    every other test in the session."""
    real_rotate, real_beat = cglib.rotate_log, events.start_heartbeat
    cglib.rotate_log = lambda *a, **k: None
    events.start_heartbeat = lambda *a, **k: None
    try:
        yield
    finally:
        cglib.rotate_log, events.start_heartbeat = real_rotate, real_beat


class FakeHandle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


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


class FakeHid:
    """Stands in for the hid module, recording every enumerate() so a test can
    assert the listener never LOOKED at the device."""

    def __init__(self, interfaces=3):
        self.enumerations = 0
        self.opened = []
        self.interfaces = interfaces

    def enumerate(self, vid, pid):
        self.enumerations += 1
        return [{"path": f"p{i}".encode()} for i in range(self.interfaces)]

    def device(self):
        h = FakeHandle()
        h.open_path = lambda path: None
        h.set_nonblocking = lambda v: None
        h.read = lambda n: []            # never any input reports
        self.opened.append(h)
        return h


def drive(lock_age_s, stop_after, session_starts_at=None, error_age_s=None):
    """Run main()'s loop, counting sleeps. Returns (captured log, fake hid).
    lock_age_s seeds the lock; session_starts_at makes one appear mid-loop, so
    the standoff transition is exercised, not just the steady states.
    error_age_s seeds a failure marker of that age - FakeHid never returns a
    report, so this is the Puck sitting untouched."""
    fresh_state(lock_age_s)
    if error_age_s is not None:
        cglib.LAST_ERROR.write_text('Enter exited without READY')
        when = time.time() - error_age_s
        os.utime(cglib.LAST_ERROR, (when, when))
    cap = cglib.CapturingLog("listener")
    fake = FakeHid()
    ticks = [0]

    def fake_sleep(_s):
        ticks[0] += 1
        if session_starts_at is not None and ticks[0] == session_starts_at:
            fresh_state(0)            # a launch just took the lock
        if ticks[0] >= stop_after:
            raise Stop()

    cl.log, cl.hid, cl.time.sleep = cap, fake, fake_sleep
    # Check the lock every pass: fake_sleep advances no real clock, and this
    # is about the transition, not the poll rate.
    cl.STANDOFF_POLL_S = cl.FAIL_CHECK_S = 0
    try:
        cl.main()
    except Stop:
        pass
    finally:
        cl.time.sleep, cl.STANDOFF_POLL_S = _real_sleep, _real_poll
        cl.FAIL_CHECK_S = _real_fail_check
    return cap, fake


def test_standoff():

    # --- session_active: the arbiter the standoff rides on -------------------
    fresh_state(None)
    assert cglib.session_active() is False, "no lock must read as free"

    fresh_state(10)
    assert cglib.session_active() is True, "a fresh lock means the Puck is spoken for"

    fresh_state(cglib.LOCK_STALE_S - 5)
    assert cglib.session_active() is True, "just inside the window is still active"

    # A lock nobody cleaned up must not permanently deafen the chord lane.
    fresh_state(cglib.LOCK_STALE_S + 5)
    assert cglib.session_active() is False, "a stale lock must read as free"

    # --- stand_off: lets go once, reports only the transition ----------------
    puck, h = held_puck()
    assert puck.stand_off() is True, "first call must let go"
    assert h.closed, "the handle was not actually closed"
    assert puck.handles == [] and puck.active is None, "state not cleared"
    assert puck.stand_off() is False, "already off the device - must not re-log"

    # A Puck that never held anything is already standing off.
    assert cl.Puck().stand_off() is False

    # --- the two composed: a live session leaves nothing open ----------------
    for age in (0, 10, cglib.LOCK_STALE_S - 1):
        fresh_state(age)
        puck, h = held_puck()
        if cglib.session_active():
            puck.stand_off()
        assert puck.handles == [], f"still holding the Puck at lock age {age}"
        assert h.closed, f"handle left open at lock age {age}"

    # --- the real loop, wired as it ships ------------------------------------
    # A session already owns the Puck: the listener must not enumerate HID.
    cap, fake = drive(lock_age_s=10, stop_after=6)
    assert fake.enumerations == 0, "stood off but still went looking for the Puck"
    assert "puck_present" not in cap.events(), cap.events()
    assert "puck_standoff" not in cap.events(), \
        "held nothing - a transition must not be logged, or a session spams it"

    # Idle: no lock, so it opens and listens like always.
    cap, fake = drive(lock_age_s=None, stop_after=6)
    assert fake.enumerations >= 1, "idle listener never opened the Puck"
    assert cap.events()[0] == "puck_present", cap.events()

    # A stale lock is NOT a session - the listener must come back.
    cap, fake = drive(lock_age_s=cglib.LOCK_STALE_S + 5, stop_after=6)
    assert fake.enumerations >= 1, "a stale lock left the chord lane deaf"

    # THE transition: listening, then a launch takes the lock mid-loop.
    cap, fake = drive(lock_age_s=None, stop_after=14, session_starts_at=4)
    ev = cap.events()
    assert ev[0] == "puck_present" and "puck_standoff" in ev, ev
    assert ev.index("puck_standoff") > ev.index("puck_present"), ev
    assert all(h.closed for h in fake.opened), "handles left open through the claim"
    assert fake.enumerations == 1, \
        f"re-opened the Puck {fake.enumerations - 1}x mid-session - the standoff leaks"
    assert ev.count("puck_standoff") == 1, f"logged the transition {ev.count('puck_standoff')}x"

    # The loop itself must reach the age-out while the Puck says nothing:
    # FakeHid never returns a report, which is the state a voice- or
    # phone-started launch fails in.
    cap, _ = drive(lock_age_s=None, stop_after=6,
                   error_age_s=cl.ERR_STALE_S + 60)
    assert 'stale_error_discarded' in cap.events(), cap.events()
    assert not cglib.LAST_ERROR.exists(), 'the loop never aged the marker out'
    # A launch started by voice or from a phone leaves nobody holding the
    # Puck. The age-out must not be gated on the buzz, or the marker
    # strands until the next successful launch (2026-08-30, stranded 5 h).
    fresh_state()
    cap = cglib.CapturingLog('listener')
    cl.log = cap
    cglib.LAST_ERROR.write_text('Enter exited without READY')
    stale = time.time() - cl.ERR_STALE_S - 60
    os.utime(cglib.LAST_ERROR, (stale, stale))
    cl.signal_last_error(None)
    assert cap.find('stale_error_discarded'), cap.events()
    assert not cglib.LAST_ERROR.exists(), 'stale marker survived unheld'

    # Fresh and unheld: kept, so the next hand on the Puck still feels it.
    cglib.LAST_ERROR.write_text('Enter exited without READY')
    cap.records.clear()
    cl.signal_last_error(None)
    assert not cap.events(), cap.events()
    assert cglib.LAST_ERROR.exists(), 'fresh marker discarded unheld'
    print("OK - standoff: fresh lock lets go of the Puck, stale lock recovers, "
          "stand_off reports only the transition, a stale failure marker ages "
          "out with nobody holding the Puck; driving main() confirms a live "
          "session never enumerates HID and a mid-loop launch closes every handle")
