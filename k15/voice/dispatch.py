"""One set of hands: every voice-triggered side effect lives here.
GrammarGate (Tier 1) and the assistant's tools (Tier 2) call the same
functions - there is no second dispatch path to drift.

Actions return a Result the caller acks with: `earcon` names the count-coded
tone to play, `detail` explains what happened. Tier 1 acks with the earcon
alone (speech there would cost a TTS round trip), so `detail` is BOTH the log
line and the only thing the assistant lane's tools report back to the model -
write it as an explanation a stranger could act on, never a bare status code.

dry_run=True logs intent instead of acting - the blind-test mode. The lock
check stays live even then (local, deterministic, side-effect-free).
"""
import subprocess
import sys
import time
from collections import namedtuple

import cglib
import couch
import events
import library
# couch.ssh / couch.ssh_intent are reached through the MODULE, never imported
# by name. One ssh implementation and, just as importantly, ONE SEAM: swapping
# couch.ssh intercepts every verb, including the mutating ones that leave via
# ssh_intent. `from couch import ssh` would give this module a second binding
# that such a swap silently misses - which is exactly how the blind suite came
# to be testing an unpatched path.

COUCH = cglib.BASE / "couch.py"

Result = namedtuple("Result", "ok earcon detail")


def _ok(detail, earcon="ok"):
    return Result(True, earcon, detail)


def _busy(detail):
    return Result(False, "busy", detail)


def _fail(detail):
    return Result(False, "fail", detail)


def _name(appid):
    """appid -> installed title, falling back to the bare id. Never raises:
    the index is a cache (empty on a fresh K15, stale after an install), and
    a naming miss must never turn a working launch into a failure."""
    try:
        appid = int(appid)
    except (TypeError, ValueError):
        return f"app {appid}"
    return library.installed_name(appid) or f"app {appid}"


class Dispatch:
    def __init__(self, cfg, log, dry_run=False):
        self.cfg = cfg
        self.voice = cfg["voice"]
        self.log = log
        self.dry_run = dry_run
        # The utterance currently being acted on. GrammarGate writes this the
        # moment it mints the id, and BOTH tiers read it from here rather than
        # from events.current() - the ambient copy does not survive the hop
        # from the frame processor's task to whichever task calls us. One
        # Dispatch per session (run_session builds it), so an attribute is
        # exactly as isolated as the ContextVar it stands in for.
        self.turn = None

    # -- internals -------------------------------------------------------------

    def _would(self, what):
        self.log("dry_run_would", action=what)
        return _ok(f"dry-run: {what}")

    def _exlink(self, what, frame_hex):
        """TV serial send; COM-port contention retry lives in cglib so
        couch.py's power/input sends get the same protection."""
        if self.dry_run:
            return self._would(f"exlink {what} ({frame_hex})")
        try:
            ack = cglib.exlink_send_hex(frame_hex, self.cfg["tvComPort"])
            self.log("exlink_send", cmd=what, ack=ack or "no-ack")
            return _ok(f"exlink {what}")
        except Exception as e:
            self.log.error("exlink_nak", cmd=what, err=str(e))
            return _fail(f"the TV command failed ({what}: {e})")

    # -- session ---------------------------------------------------------------

    def start_session(self, appid=None):
        """Same arbiter as the chord: a fresh lock means busy, never a double
        launch. couch.py owns the whole sequence (and the one rule)."""
        age = cglib.lock_age()
        if cglib.session_active(age):
            self.log("start_refused", reason="lock_fresh", lock_age_s=round(age))
            return _busy("a session is already active or starting")
        what = f"couch.py start{f' {appid}' if appid else ''}"
        if self.dry_run:
            return self._would(what)
        args = [sys.executable, str(COUCH), "start"] + ([str(appid)] if appid else [])
        # couch.py runs in its own console, so the id has to travel as an
        # argument - a ContextVar does not survive a process boundary. It does
        # not survive a TASK boundary either, which is why self.turn leads and
        # the ambient value is only the fallback (see __init__). Without it
        # couch.py mints its own id and the user's sentence is joined to the
        # launch it caused by nothing but a clock reading.
        turn = self.turn or events.current().get("turn")
        if turn:
            args += ["--turn", turn]
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.log("session_dispatched", appid=appid, turn=turn)
        return _ok(f"starting a session ({what})")

    def end_session(self):
        """Works mid-game and mid-launch alike (teardown wins - Exit stops a
        running Enter on the host)."""
        if self.dry_run:
            return self._would("ssh exit")
        try:
            out = couch.ssh_intent("exit", turn=self.turn)
        except Exception as e:
            self.log.error("end_session_failed", err=str(e), turn=self.turn)
            return _fail(f"couldn't reach the PC (ssh exit: {e})")
        if out == "OK":
            self.log("end_session_dispatched", turn=self.turn)
            return _ok("ending the session")
        self.log.warn("end_session_refused", answer=out, turn=self.turn)
        return _fail(f"the PC refused the exit (ssh exit: {out})")

    def now_playing(self):
        """RunningAppID via the `playing` verb. The one Result whose detail is
        data rather than prose - assistant.get_now_playing parses it."""
        if self.dry_run:
            return self._would("ssh playing")
        try:
            out = couch.ssh("playing").strip()
        except Exception as e:
            return _fail(f"couldn't reach the PC (ssh playing: {e})")
        return _ok(out if out.isdigit() else "0")

    def play_game(self, appid):
        """Session live -> direct host launch (Dispatch verb answers
        truthfully: OK/ALREADY/BUSY/NOTREADY). No session -> full couch
        launch with the game queued for after READY."""
        if not cglib.session_active():
            return self.start_session(appid)
        if self.dry_run:
            return self._would(f"ssh launch {appid}")
        try:
            out = couch.ssh_intent(f"launch {appid}")
        except Exception as e:
            self.log.error("launch_failed", appid=appid, err=str(e))
            return _fail(f"couldn't reach the PC (ssh launch: {e})")
        self.log("launch_dispatched", appid=appid, answer=out)
        if out == "OK":
            return _ok(f"launching {_name(appid)}")
        if out == "ALREADY":
            return _ok(f"{_name(appid)} is already running")
        if out.startswith("BUSY:"):
            # Name the blocker. The assistant lane sees only `detail`, and a
            # bare appid there leaves it unable to tell the user WHAT to quit
            # - it would just say "something else is running". The raw code
            # stays for the log.
            return _busy(f"{_name(out.split(':', 1)[1])} is already running - "
                         f"it has to be quit with the controller first ({out})")
        if out == "NOTREADY":
            # Lock fresh but host pre-READY: a launch is in flight.
            return _busy("the session is still starting")
        if out == "NOTINSTALLED":
            return _fail(f"{_name(appid)} is not installed - "
                         "installing it needs the controller")
        return _fail(f"the launch failed (ssh launch: {out})")

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
            self.log("volume_clamped", asked=int(level), set=clamped, max=vmax)
        return self._exlink(f"vol_set {clamped}", cglib.vol_set_frame(clamped))

    def mute_toggle(self):
        """Blind toggle, permanently: the S90C exposes no discrete mute
        on/off, and the decode drill proved its status query returns a
        constant canned echo (byte-identical across volume/mute states) -
        there is no state to read, so none is tracked. The vocabulary is
        'mute' = toggle, with vol_set as the resync."""
        return self._exlink("mute_toggle", cglib.EXLINK_FRAMES["mute_toggle"])

    def switch_input(self, spoken_name):
        """Config owns the spoken-name -> input map. The GAMING input means
        "get me gaming": with no session it STARTS one (identical UX to
        "start a session" - refusing with a hint was worse than doing the
        thing); mid-launch it answers "still starting"; with a READY session it
        flips instantly. The one rule is untouched either way - couch.py
        switches the input only at READY, so nothing dead is ever shown. Other
        inputs switch freely, like a remote."""
        cmd = self.voice["inputs"].get(spoken_name.strip().lower())
        if cmd is None:
            return _fail(f"there is no input called '{spoken_name}'")
        if cmd == self.cfg["tvGamingCmd"]:
            if not cglib.session_active():
                # Local lock check first: a sleeping PC costs no ssh timeout
                # before the launch kicks off.
                self.log("input_starts_session", input=cmd)
                return self.start_session()
            if self.dry_run:
                return self._would(f"check READY then exlink {cmd}")
            try:
                if couch.ssh("status") == "NOTREADY":
                    self.log("input_deferred", input=cmd, reason="not_ready")
                    return _busy("the session is still starting - the TV will "
                                 "switch over on its own when it's ready")
            except Exception as e:
                self.log.error("input_refused", input=cmd, err=str(e))
                return _fail(f"couldn't reach the PC (status check: {e})")
        frame_hex = cglib.EXLINK_FRAMES.get(cmd)
        if frame_hex is None:
            return _fail(f"that input isn't configured correctly - config maps "
                         f"'{spoken_name}' to unknown command '{cmd}'")
        return self._exlink(f"input {cmd}", frame_hex)
