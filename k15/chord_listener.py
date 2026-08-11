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

cglib.rotate_log()
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

puck, held, armed = Puck(), None, False
last_busy = 0.0
last_err_check = 0.0
while True:
    if not puck.handles:
        if not puck.open_all():
            time.sleep(1)                        # Puck truly absent (session active)
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
                if age is not None and age < cglib.LOCK_STALE_S:
                    # A launch/session already owns the lock - couch.py would
                    # refuse anyway; say "busy", don't promise a launch.
                    if time.time() - last_busy >= BUSY_COOLDOWN_S:
                        log("chord_busy", lock_age_s=round(age))
                        buzz(puck.active, cglib.PATTERN_BUSY, "busy")
                        last_busy = time.time()
                    held = None
                else:
                    # The chord is where this intent begins, so the id is
                    # minted here and handed to couch.py - which passes it on
                    # to the gaming PC. One chord, one story, two machines.
                    turn = events.new_turn()
                    log("chord", turn=turn)
                    buzz(puck.active, cglib.PATTERN_LAUNCH, "launch")
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