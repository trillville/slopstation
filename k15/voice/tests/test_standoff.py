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
import chord_listener as cl


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

    print("OK - standoff: fresh lock lets go of the Puck, stale lock recovers, "
          "stand_off closes once and reports only the transition")


if __name__ == "__main__":
    main()
