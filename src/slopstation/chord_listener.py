import subprocess, sys, time
import hid

from slopstation import checkin
from slopstation import cglib
from slopstation import events
from slopstation import haptics
from slopstation.haptics import RID_INPUT, VID, PID

BTN_BYTE = 4
CHORD    = 0x01 | 0x80            # Steam + right-trigger click
HOLD_S   = 2.0
# An argv prefix, not a path: the package is installed, so there is no script
# on disk to point at.
COUCH    = [sys.executable, "-m", "slopstation.couch"]

# Re-bench after controller firmware updates.
HAPTIC_GAIN     = 0     # s8 dB-ish; 0 = natural level, 120 = clamped max
BUSY_COOLDOWN_S = 5.0   # a held chord re-validates every ~2s; don't machine-gun the busy buzz
FAIL_CHECK_S    = 2.0   # how often to look for couch.py's last_error marker
PARTIAL_COOLDOWN_S = 10.0  # rate limit for chord_partial; an idle hand on the
                        # controller must not flood the lane
ERR_STALE_S     = 600   # failures older than this are history, not news
STANDOFF_POLL_S = 0.5   # how often to ask the lock whether the Puck is spoken for

log = cglib.make_log("listener")


def buzz(dev, pattern, what):
    """Best-effort: a haptic failure must never delay or block anything."""
    try:
        haptics.play_pattern(dev, pattern, HAPTIC_GAIN)
        log("buzz_sent", pattern=what)
        return True
    except Exception as e:
        log.warn("buzz_failed", pattern=what, err=str(e))
        return False


def signal_last_error(dev):
    """couch.py writes state/last_error when a launch dies; tell the hands.
    The marker is consumed on a successful buzz, retained for retry otherwise,
    discarded past ERR_STALE_S.

    dev is None when the Puck is reporting nothing, and the age-out still
    runs: a launch started by voice or from a phone leaves nobody holding the
    controller, so a buzz-gated discard would strand the marker until the next
    successful launch (2026-08-30, stranded 5 h)."""
    try:
        age = time.time() - cglib.LAST_ERROR.stat().st_mtime
    except OSError:
        return
    if age > ERR_STALE_S:
        cglib.LAST_ERROR.unlink(missing_ok=True)
        log("stale_error_discarded", age_s=round(age))
        return
    if dev is None:
        return                          # nobody is holding it; keep for retry
    if buzz(dev, haptics.PATTERN_FAIL, "fail"):
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
        """Let go of the device if we hold it. True only on the TRANSITION, so
        the caller logs once rather than every poll of a session."""
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
    # Liveness for the load-bearing lane; a deaf chord is only discovered from
    # the couch. A thread rather than a check in the read loop: the loop can
    # block in hid.read or the 3 s stand-by sleep, and a heartbeat that stopped
    # while the Puck is claimed would page during every normal session.
    events.start_heartbeat("listener")
    # The heartbeat proves the lane is alive TO THE SHIPPER; this proves it to
    # Sentry directly, so a dead collector cannot make a dead lane look
    # healthy. No-ops without a sentryDsn.
    checkin.start("listener")

    puck, held, armed = Puck(), None, False
    last_busy = 0.0
    last_partial = 0.0
    last_err_check = 0.0
    last_session_check = 0.0
    standoff = False
    while True:
        # HANDS OFF while a session owns the Puck, launch included. The claim
        # is the fragile moment: VirtualHere unbinds the Puck from the K15's
        # HID stack to forward it, and reading 13 interfaces at 200 Hz through
        # that handoff is a plausible (unproven) cause of the controller that
        # enumerates, rumbles, then ignores every button. The chord path closes
        # before it dispatches; a VOICE launch is another process, so the
        # session lock is the signal - couch.py holds it from ~10 s before the
        # claim.
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
                    # Backstop, not the main gate - the standoff above
                    # normally catches a fresh lock first. Covers a launch
                    # started inside that window, which couch.py would refuse.
                    if cglib.session_active(age):
                        if time.time() - last_busy >= BUSY_COOLDOWN_S:
                            log("chord_busy", lock_age_s=round(age))
                            buzz(puck.active, haptics.PATTERN_BUSY, "busy")
                            last_busy = time.time()
                        held = None
                    else:
                        # The intent begins here, so the turn id is minted
                        # here and handed to couch.py, which passes it on to
                        # the gaming PC.
                        turn = events.new_turn()
                        log("chord", turn=turn)
                        buzz(puck.active, haptics.PATTERN_LAUNCH, "launch")
                        # The only silence guaranteed BEFORE couch.py has
                        # started python and taken the lock.
                        puck.close()
                        subprocess.Popen(COUCH + ["start", "--turn", turn],
                                         creationflags=subprocess.CREATE_NEW_CONSOLE)
                        time.sleep(20); held, armed = None, False
            else:
                held = None
                # Separates a Puck nobody touched from one that enumerates,
                # rumbles and never reports a button - which otherwise needs
                # calibrate.py to tell apart (2026-08-19). The BYTE, not the
                # count: whether buttons arrive, and whether they still land
                # where CHORD expects after a firmware update.
                b = r[BTN_BYTE] if len(r) > BTN_BYTE else 0
                if b and time.time() - last_partial >= PARTIAL_COOLDOWN_S:
                    log("chord_partial", btn=f"{b:02x}", want=f"{CHORD:02x}")
                    last_partial = time.time()
        if time.time() - last_err_check >= FAIL_CHECK_S:
            last_err_check = time.time()
            signal_last_error(puck.active if armed else None)
        time.sleep(0.005)


if __name__ == "__main__":
    main()
