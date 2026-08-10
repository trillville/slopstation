"""One set of hands: every voice-triggered side effect lives here.
GrammarGate (Tier 1) and the assistant's tools (Tier 2, C3) call the same
functions - there is no second dispatch path to drift.

Actions return a Result the caller acks with: `earcon` names the count-coded
tone to play, `say` (C3) is optional speech, `detail` goes to the log.

dry_run=True logs intent instead of acting - the blind-test mode. The lock
check stays live even then (local, deterministic, side-effect-free).
"""
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cglib
from couch import ssh          # doctor.py precedent: one ssh implementation

COUCH = cglib.BASE / "couch.py"

Result = namedtuple("Result", "ok earcon say detail")


def _ok(detail, say=None, earcon="ok"):
    return Result(True, earcon, say, detail)


def _busy(detail, say=None):
    return Result(False, "busy", say, detail)


def _fail(detail, say=None):
    return Result(False, "fail", say, detail)


class Dispatch:
    def __init__(self, cfg, log, dry_run=False):
        self.cfg = cfg
        self.voice = cfg["voice"]
        self.log = log
        self.dry_run = dry_run

    # -- internals -------------------------------------------------------------

    def _would(self, what):
        self.log(f"DRY-RUN would: {what}")
        return _ok(f"dry-run: {what}")

    def _exlink(self, what, frame_hex):
        """TV serial send with one retry - couch.py shares the COM port in
        open-write-close bursts, so a transient open failure gets one second
        of patience before it becomes a fail earcon."""
        if self.dry_run:
            return self._would(f"exlink {what} ({frame_hex})")
        for attempt in (1, 2):
            try:
                ack = cglib.exlink_send_hex(frame_hex, self.cfg["tvComPort"])
                self.log(f"exlink {what} -> {ack or 'no-ack'}")
                return _ok(f"exlink {what}")
            except Exception as e:
                if attempt == 1:
                    time.sleep(1)
                else:
                    self.log(f"exlink {what} FAILED: {e}")
                    return _fail(f"exlink {what}: {e}",
                                 say="The TV command failed.")

    # -- session ---------------------------------------------------------------

    def start_session(self, appid=None):
        """Same arbiter as the chord: a fresh lock means busy, never a double
        launch. couch.py owns the whole sequence (and the one rule)."""
        age = cglib.lock_age()
        if age is not None and age < cglib.LOCK_STALE_S:
            self.log(f"start refused - session lock is fresh ({age:.0f}s)")
            return _busy("session already active/starting",
                         say="A session is already running.")
        what = f"couch.py start{f' {appid}' if appid else ''}"
        if self.dry_run:
            return self._would(what)
        args = [sys.executable, str(COUCH), "start"] + ([str(appid)] if appid else [])
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.log(f"dispatched {what}")
        return _ok(what, say="Starting a session.")

    def end_session(self):
        """Works mid-game (the exit asymmetry, closed) and mid-launch
        (teardown wins - Exit stops a running Enter on the host)."""
        if self.dry_run:
            return self._would("ssh exit")
        try:
            out = ssh("exit")
        except Exception as e:
            self.log(f"end session failed: {e}")
            return _fail(f"ssh exit: {e}", say="I couldn't reach the PC.")
        if out == "OK":
            self.log("end session dispatched")
            return _ok("ssh exit", say="Ending the session.")
        self.log(f"end session refused: {out}")
        return _fail(f"ssh exit: {out}", say="The PC refused the exit.")

    # -- TV --------------------------------------------------------------------

    def volume_up(self):
        step = int(self.voice.get("volumeStep", 5))
        if self.dry_run:
            return self._would(f"vol_up x{step}")
        for _ in range(step):
            r = self._exlink("vol_up", cglib.EXLINK_FRAMES["vol_up"])
            if not r.ok:
                return r
            time.sleep(0.05)
        return _ok(f"vol_up x{step}")

    def volume_down(self):
        step = int(self.voice.get("volumeStep", 5))
        if self.dry_run:
            return self._would(f"vol_down x{step}")
        for _ in range(step):
            r = self._exlink("vol_down", cglib.EXLINK_FRAMES["vol_down"])
            if not r.ok:
                return r
            time.sleep(0.05)
        return _ok(f"vol_down x{step}")

    def volume_set(self, level):
        """Absolute set, clamped to volumeMax - a misheard number must never
        blast the room. Also the mute-desync escape hatch."""
        vmax = int(self.voice.get("volumeMax", 40))
        clamped = max(0, min(vmax, int(level)))
        if clamped != int(level):
            self.log(f"vol_set {level} clamped to {clamped} (volumeMax {vmax})")
        return self._exlink(f"vol_set {clamped}", cglib.vol_set_frame(clamped))

    def mute_toggle(self):
        """Blind toggle: the S90C exposes no discrete mute on/off, and query
        support is unproven until the C1 probe - no state is tracked, so the
        vocabulary is 'mute' = toggle, with vol_set as the resync."""
        return self._exlink("mute_toggle", cglib.EXLINK_FRAMES["mute_toggle"])

    def switch_input(self, spoken_name):
        """Config owns the spoken-name -> input map. Switching to the GAMING
        input is READY-gated: automation never shows a dead input (the one
        rule); other inputs switch freely, like a remote would."""
        cmd = self.voice["inputs"].get(spoken_name.strip().lower())
        if cmd is None:
            return _fail(f"unknown input '{spoken_name}'",
                         say=f"I don't know the input {spoken_name}.")
        if cmd == self.cfg["tvGamingCmd"]:
            if self.dry_run:
                return self._would(f"check READY then exlink {cmd}")
            try:
                if ssh("status") == "NOTREADY":
                    self.log(f"input {cmd} refused - host not READY")
                    return _fail("gaming input while not READY",
                                 say="The PC isn't running a session - "
                                     "say 'start a session' instead.")
            except Exception as e:
                self.log(f"input {cmd} refused - status check failed ({e})")
                return _fail(f"status check: {e}", say="I couldn't reach the PC.")
        return self._exlink(f"input {cmd}", cglib.EXLINK_FRAMES[cmd])
