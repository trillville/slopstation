"""Blind test: the chord listener's hands-off rule.

The listener must hold NO handles on the Puck for as long as a session owns it.
A chord launch always got that (it closes before it dispatches); a VOICE launch
dispatches from another process and could not ask, so the K15 kept reading 13
interfaces while VirtualHere handed the device to the gaming PC - the
enumerated-but-dead controller. Both trigger paths now ride the session lock.

Two properties, one per risk:
  * a fresh lock stands the listener off       - or the bug is not fixed
  * a STALE lock reads as free                 - or the chord lane goes deaf
    until someone deletes a file, which is far worse than the bug

Run:  python tests\\test_standoff.py     (system python - no venv needed)
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# The listener runs on SYSTEM python and `hid` is deliberately not in the voice
# venv (see requirements.txt), so stub it: nothing here tests HID, only the
# decision to let go of it.
sys.modules.setdefault("hid", types.SimpleNamespace(
    enumerate=lambda *a, **k: [], device=object))

import cglib
import events
import chord_listener as cl

_real_sleep = time.sleep
_real_poll = cl.STANDOFF_POLL_S

# main() owns two startup side effects that belong to RUNNING the lane, not to
# exercising its loop: a log rotation and a heartbeat thread.
cglib.rotate_log = lambda *a, **k: None
events.start_heartbeat = lambda *a, **k: None


def with_temp_lock(age_s):
    """Point cglib.LOCK at a temp file with the given age; None = absent."""
    tmp = Path(tempfile.mkdtemp()) / "session.lock"
    if age_s is not None:
        tmp.write_text("x")
        old = time.time() - age_s
        os.utime(tmp, (old, old))
    cglib.LOCK = tmp


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
# The units above can all pass while main() wires them together wrongly, which
# is the one thing a static check cannot catch after an indentation-heavy
# refactor of the load-bearing lane. So the loop gets driven for real: a fake
# hid module, and a fake sleep that counts iterations, can start a session
# part-way through, and finally breaks out.

class Stop(Exception):
    """Not OSError/ValueError - the loop's own handler must not swallow it."""


class FakeHid:
    """Stands in for the hid module. Records every enumerate() so a test can
    assert the listener never even LOOKED at the device."""

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


def drive(lock_age_s, stop_after, session_starts_at=None):
    """Run main()'s loop, counting sleeps. Returns (captured log, fake hid).

    lock_age_s seeds the lock; session_starts_at makes one appear mid-loop, so
    the standoff TRANSITION is exercised and not just the steady states."""
    with_temp_lock(lock_age_s)
    cap = cglib.CapturingLog("listener")
    fake = FakeHid()
    ticks = [0]

    def fake_sleep(_s):
        ticks[0] += 1
        if session_starts_at is not None and ticks[0] == session_starts_at:
            with_temp_lock(0)            # a launch just took the lock
        if ticks[0] >= stop_after:
            raise Stop()

    cl.log, cl.hid, cl.time.sleep = cap, fake, fake_sleep
    # Check the lock every pass: this test is about the transition, not the
    # poll rate, and fake_sleep advances no real clock for the rate to use.
    cl.STANDOFF_POLL_S = 0
    try:
        cl.main()
    except Stop:
        pass
    finally:
        cl.time.sleep, cl.STANDOFF_POLL_S = _real_sleep, _real_poll
    return cap, fake


def main():
    # --- session_active: the arbiter the standoff rides on -------------------
    with_temp_lock(None)
    assert cglib.session_active() is False, "no lock must read as free"

    with_temp_lock(10)
    assert cglib.session_active() is True, "a fresh lock means the Puck is spoken for"

    with_temp_lock(cglib.LOCK_STALE_S - 5)
    assert cglib.session_active() is True, "just inside the window is still active"

    # THE deafness backstop. If this ever reads True, a lock nobody cleaned up
    # takes the load-bearing lane down permanently - asserted, never assumed.
    with_temp_lock(cglib.LOCK_STALE_S + 5)
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
    # This is the invariant the voice launch used to violate.
    for age in (0, 10, cglib.LOCK_STALE_S - 1):
        with_temp_lock(age)
        puck, h = held_puck()
        if cglib.session_active():
            puck.stand_off()
        assert puck.handles == [], f"still holding the Puck at lock age {age}"
        assert h.closed, f"handle left open at lock age {age}"

    # --- the real loop, wired as it ships ------------------------------------
    # A session already owns the Puck: the listener must not so much as
    # enumerate HID. This is the assertion the voice launch used to fail.
    cap, fake = drive(lock_age_s=10, stop_after=6)
    assert fake.enumerations == 0, "stood off but still went looking for the Puck"
    assert "puck_present" not in cap.events(), cap.events()
    assert "puck_standoff" not in cap.events(), \
        "held nothing - a transition must not be logged, or a session spams it"

    # Idle: no lock, so it opens and listens like always.
    cap, fake = drive(lock_age_s=None, stop_after=6)
    assert fake.enumerations >= 1, "idle listener never opened the Puck"
    assert cap.events()[0] == "puck_present", cap.events()

    # A stale lock is NOT a session - the listener must come back. The deafness
    # backstop again, this time through the loop rather than the predicate.
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

    print("OK - standoff: fresh lock lets go of the Puck, stale lock recovers, "
          "stand_off reports only the transition; driving main() confirms a live "
          "session never enumerates HID and a mid-loop launch closes every handle")


if __name__ == "__main__":
    main()
