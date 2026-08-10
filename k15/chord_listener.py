import subprocess, sys, time
import hid

import cglib
from cglib import VID, PID

RID_INPUT = 0x42                  # input report type, from calibrate.py ("report type: 42")
BTN_BYTE = 4
CHORD    = 0x01 | 0x80            # Steam + right-trigger click
HOLD_S   = 2.0
COUCH    = cglib.BASE / "couch.py"

# Haptic "chord heard" ack - two quick rising tones on both trackpads, played
# through cglib.play_pattern (the same engine haptic_test's audition/quiz use,
# so the bench feel IS the production feel). Re-bench after firmware updates.
ACK_PATTERN = ((440, 60, 10, 0, 0), (660, 90, 0, 0, 0))   # (freq, dur_ms, gap_ms, lfo_f, lfo_d)
ACK_GAIN    = 0                      # s8 dB-ish; 0 = natural level (bench-tuned 2026-08-10; 120 = clamped max)

cglib.rotate_log()
log = cglib.make_log("listener")


def ack_chirp(dev):
    """Best-effort by rule: a haptic failure must never delay or block the
    launch. Writes go to the already-latched input interface (same target the
    SDL driver and SteamControllerBridge use)."""
    try:
        cglib.play_pattern(dev, ACK_PATTERN, ACK_GAIN)
        log("ack chirp sent")
    except Exception as e:
        log(f"ack chirp failed ({e}) - launching anyway")

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
while True:
    if not puck.handles:
        if not puck.open_all():
            time.sleep(1)                        # Puck truly absent (session active)
            continue
        log(f"Puck present - {len(puck.handles)} interfaces open, waiting for controller")
        held, armed = None, False
    try:
        r = puck.read_input()
    except (OSError, ValueError):
        log("device vanished (claimed) - standing by")
        puck.close(); time.sleep(3); continue
    if r:
        if not armed:
            log(f"input stream found (type {RID_INPUT:02x}) - armed")
            armed = True
        if len(r) > BTN_BYTE and (r[BTN_BYTE] & CHORD) == CHORD:
            held = held or time.time()
            if time.time() - held >= HOLD_S:
                log("chord! launching session")
                ack_chirp(puck.active)
                puck.close()
                subprocess.Popen([sys.executable, str(COUCH), "start"],
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
                time.sleep(20); held, armed = None, False
        else:
            held = None
    time.sleep(0.005)