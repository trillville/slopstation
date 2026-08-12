import subprocess, sys, time
import hid

import cglib
import events
from cglib import VID, PID

RID_INPUT = 0x42                  # input report type, from calibrate.py ("report type: 42")
BTN_BYTE = 4
CHORD    = 0x01 | 0x80            # Steam + right-trigger click
HOLD_S   = 2.0
COUCH    = cglib.BASE / "couch.py"

# Haptic vocabulary (patterns in cglib, played via the same engine haptic_test's
# audition uses): count is the message - 1 thud launch, 2 busy, 3 fail.
# Re-bench after controller firmware updates.
HAPTIC_GAIN     = 0     # s8 dB-ish; 0 = natural level, 120 = clamped max
BUSY_COOLDOWN_S = 5.0   # a held chord re-validates every ~2s; don't machine-gun the busy buzz
FAIL_CHECK_S    = 2.0   # how often to look for couch.py's last_error marker
ERR_STALE_S     = 600   # failures older than this are history, not news
STANDOFF_POLL_S = 0.5   # how often to ask the lock whether the Puck is spoken for

log = cglib.make_log("listener")


def buzz(dev, pattern, what):
    """Best-effort by rule: a haptic failure must never delay or block anything."""
    try:
        cglib.play_pattern(dev, pattern, HAPTIC_GAIN)
        log("buzz_sent", pattern=what)
        return True
    except Exception as e:
        log.warn("buzz_failed", pattern=what, err=str(e))
        return False


def signal_last_error(dev):
    """couch.py writes state/last_error when a launch dies; tell the hands.
    Marker is consumed on a successful buzz, retained for retry otherwise,
    and discarded once it's too old to be news."""
    try:
        age = time.time() - cglib.LAST_ERROR.stat().st_mtime
    except OSError:
        return
    if age > ERR_STALE_S:
        cglib.LAST_ERROR.unlink(missing_ok=True)
        log("stale_error_discarded", age_s=round(age))
        return
    if buzz(dev, cglib.PATTERN_FAIL, "fail"):
        try:
            reason = cglib.LAST_ERROR.read_text().strip()
        except OSError:
            reason = "?"
        cglib.LAST_ERROR.unlink(missing_ok=True)
        log("launch_failure_signaled", reason=reason)

class Puck:
    def __init__(self): self.handles, self.active = [], None
    def open_all(self):
        self.close()
        for d in hid.enumerate(VID, PID):
            try:
                h = hid.device(); h.open_path(d["path"]); h.set_nonblocking(True)
                self.handles.append(h)
            except (OSError, ValueError): pass
        return bool(self.handles)
    def close(self):
        for h in self.handles:
            try: h.close()
            except Exception: pass
        self.handles, self.active = [], None
    def stand_off(self):
        """Let go of the device if we hold it. Returns True only on the
        TRANSITION, so the caller logs the moment we let go rather than once
        per poll for the length of a session."""
        if not self.handles:
            return False
        self.close()
        return True
    def read_input(self):
        """Return the next report of type RID_INPUT, or None. Latch onto the
        interface that produces them; ignore all other report streams."""
        if self.active is not None:
            while True:
                r = self.active.read(64)         # raises if claimed/unplugged
                if not r: return None
                if r[0] == RID_INPUT: return r   # drain non-input types silently
        for h in list(self.handles):
            try:
                r = h.read(64)
            except (OSError, ValueError):
                self.handles.remove(h)
                try: h.close()
                except Exception: pass
                continue
            if r and r[0] == RID_INPUT:
                self.active = h                  # THE input interface, by content
                return r
        if not self.handles:
            raise OSError("all interfaces gone")
        return None

def main():
    cglib.rotate_log()
    # Liveness for the LOAD-BEARING lane - the one whose silent death is the
    # expensive failure, because a deaf chord is only discovered from the couch.
    # A thread rather than a check in the read loop: the loop can block in
    # hid.read or sit in the 3 s stand-by sleep, and a heartbeat that stops when
    # the Puck is claimed would page during every normal session.
    events.start_heartbeat("listener")

    puck, held, armed = Puck(), None, False
    last_busy = 0.0
    last_err_check = 0.0
    last_session_check = 0.0
    standoff = False
    while True:
        # HANDS OFF for as long as a session owns the Puck, launch included.
        # The claim is the fragile moment: VirtualHere has to unbind the Puck
        # from the K15's own HID stack to forward it, and doing that while 13
        # interfaces are being read at 200 Hz is what produces the controller
        # that enumerates, rumbles, and then ignores every button.
        #
        # The chord path always had this for free - it closes before it
        # dispatches. A VOICE launch dispatches from a different process
        # entirely and had no way to ask, so the listener read straight through
        # the handoff and had its handles torn away mid-read. That asymmetry is
        # the whole bug: on turn eaa8bc the K15 was still reading at 22:57:32
        # while the PC finished claiming at 22:57:37 (puck_vanished
        # reason=claimed is that tear-away, and it appears on every voice
        # launch and no chord launch).
        #
        # The session lock is the signal both paths already share, so this
        # needs no new IPC: couch.py touches it before its first side effect,
        # ~10 s ahead of the claim.
        if time.time() - last_session_check >= STANDOFF_POLL_S:
            last_session_check = time.time()
            standoff = cglib.session_active()
        if standoff:
            if puck.stand_off():
                log("puck_standoff", reason="session_lock")
            held, armed = None, False
            time.sleep(STANDOFF_POLL_S)
            continue
        if not puck.handles:
            if not puck.open_all():
                time.sleep(1)                    # Puck truly absent (session active)
                continue
            log("puck_present", interfaces=len(puck.handles))
            held, armed = None, False
        try:
            r = puck.read_input()
        except (OSError, ValueError):
            log("puck_vanished", reason="claimed")
            puck.close(); time.sleep(3); continue
        if r:
            if not armed:
                log("armed", report_type=f"{RID_INPUT:02x}")
                armed = True
            if len(r) > BTN_BYTE and (r[BTN_BYTE] & CHORD) == CHORD:
                held = held or time.time()
                if time.time() - held >= HOLD_S:
                    age = cglib.lock_age()
                    # Backstop, not the main gate: the standoff above catches a
                    # fresh lock within STANDOFF_POLL_S and we never get here.
                    # This still covers a launch that started inside that
                    # window - couch.py would refuse anyway, so say "busy"
                    # rather than promise a launch that won't happen.
                    if age is not None and age < cglib.LOCK_STALE_S:
                        if time.time() - last_busy >= BUSY_COOLDOWN_S:
                            log("chord_busy", lock_age_s=round(age))
                            buzz(puck.active, cglib.PATTERN_BUSY, "busy")
                            last_busy = time.time()
                        held = None
                    else:
                        # The chord is where this intent begins, so the id is
                        # minted here and handed to couch.py - which passes it
                        # on to the gaming PC. One chord, one story, two
                        # machines.
                        turn = events.new_turn()
                        log("chord", turn=turn)
                        buzz(puck.active, cglib.PATTERN_LAUNCH, "launch")
                        # Kept even though the standoff now covers the launch
                        # window: this is the only silence guaranteed BEFORE
                        # couch.py has started python and touched the lock.
                        puck.close()
                        subprocess.Popen([sys.executable, str(COUCH), "start",
                                          "--turn", turn],
                                         creationflags=subprocess.CREATE_NEW_CONSOLE)
                        time.sleep(20); held, armed = None, False
            else:
                held = None
            if armed and time.time() - last_err_check >= FAIL_CHECK_S:
                last_err_check = time.time()
                signal_last_error(puck.active)
        time.sleep(0.005)


if __name__ == "__main__":
    main()
