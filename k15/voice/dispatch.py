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

import cglib
from couch import ssh          # one ssh implementation - couch.py owns it

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
        """TV serial send; COM-port contention retry lives in cglib so
        couch.py's power/input sends get the same protection."""
        if self.dry_run:
            return self._would(f"exlink {what} ({frame_hex})")
        try:
            ack = cglib.exlink_send_hex(frame_hex, self.cfg["tvComPort"])
            self.log(f"exlink {what} -> {ack or 'no-ack'}")
            return _ok(f"exlink {what}")
        except Exception as e:
            self.log(f"exlink {what} FAILED: {e}")
            return _fail(f"exlink {what}: {e}", say="The TV command failed.")

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

    def now_playing(self):
        """RunningAppID via the `playing` verb; Result.detail carries it."""
        if self.dry_run:
            return self._would("ssh playing")
        try:
            out = ssh("playing").strip()
        except Exception as e:
            return _fail(f"ssh playing: {e}", say="I couldn't reach the PC.")
        return _ok(out if out.isdigit() else "0")

    def play_game(self, appid):
        """Session live -> direct host launch (Dispatch verb answers
        truthfully: OK/ALREADY/BUSY/NOTREADY). No session -> full couch
        launch with the game queued for after READY."""
        age = cglib.lock_age()
        if age is None or age >= cglib.LOCK_STALE_S:
            return self.start_session(appid)
        if self.dry_run:
            return self._would(f"ssh launch {appid}")
        try:
            out = ssh(f"launch {appid}")
        except Exception as e:
            self.log(f"launch {appid} failed: {e}")
            return _fail(f"ssh launch: {e}", say="I couldn't reach the PC.")
        self.log(f"launch {appid} -> {out}")
        if out == "OK":
            return _ok(f"launch {appid}", say="Launching.")
        if out == "ALREADY":
            return _ok(f"{appid} already running", say="It's already running.")
        if out.startswith("BUSY:"):
            return _busy(f"another game is running ({out})",
                         say="Another game is running - quit it first.")
        if out == "NOTREADY":
            # Lock fresh but host pre-READY: a launch is in flight.
            return _busy("session launch in flight",
                         say="The session is still starting.")
        return _fail(f"ssh launch: {out}", say="The launch failed.")

    # -- TV --------------------------------------------------------------------

    def _vol_steps(self, name):
        step = int(self.voice["volumeStep"])
        if self.dry_run:
            return self._would(f"{name} x{step}")
        for _ in range(step):
            r = self._exlink(name, cglib.EXLINK_FRAMES[name])
            if not r.ok:
                return r
            time.sleep(0.05)
        return _ok(f"{name} x{step}")

    def volume_up(self):
        return self._vol_steps("vol_up")

    def volume_down(self):
        return self._vol_steps("vol_down")

    def volume_set(self, level):
        """Absolute set, clamped to volumeMax - a misheard number must never
        blast the room. Also the mute-desync escape hatch."""
        vmax = int(self.voice["volumeMax"])
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
        frame_hex = cglib.EXLINK_FRAMES.get(cmd)
        if frame_hex is None:
            return _fail(f"config maps '{spoken_name}' to unknown command '{cmd}'",
                         say="That input isn't configured correctly.")
        return self._exlink(f"input {cmd}", frame_hex)
