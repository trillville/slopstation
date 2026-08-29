"""Every voice-triggered side effect: the grammar gate and the assistant's
tools call the same functions.

Actions return a Result: `earcon` names the count-coded tone, `detail` is
both the log line and the only thing the assistant lane reports to the model
- prose a stranger could act on, not a status code.

dry_run=True logs intent instead of acting; the lock check stays live.
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections import namedtuple

import cglib
import events
import gamepc
import library
import tv
# gamepc is reached through the MODULE, never `from gamepc import ...`: a
# second binding is one the tests' patches would miss.

COUCH = cglib.BASE / "couch.py"

Result = namedtuple("Result", "ok earcon detail")

# Per-utterance context: correlation id plus the user's words (post
# wake-strip; None on lanes with no transcript - the chord, the REPL).
# Passed explicitly, not via ContextVar: a ContextVar is copied at task
# creation, so the gate setting it never reaches the assistant's tool
# dispatch, a sibling task already running. A namedtuple so the pair swaps
# atomically; a second utterance landing mid-dispatch re-points the
# attribute, so consumers snapshot at operation START (test_turn drills it).
Utterance = namedtuple("Utterance", "turn asked")


def _ok(detail: str, earcon: str = "ok") -> Result:
    return Result(True, earcon, detail)


def _busy(detail: str) -> Result:
    return Result(False, "busy", detail)


def _fail(detail: str) -> Result:
    return Result(False, "fail", detail)


def _no_task(out: str) -> Result:
    """NOTASK:<name> - a scheduled task the PC never had registered."""
    return _fail(f"the {out.split(':', 1)[1]} task isn't registered on the "
                 "gaming PC - it needs the one-time Register-ScheduledTask "
                 "from the setup guide")


def _name(appid: int | str) -> str:
    """appid -> installed title, falling back to the bare id. Never raises:
    the index is a cache (empty on a fresh K15, stale after an install)."""
    try:
        appid = int(appid)
    except (TypeError, ValueError):
        return f"app {appid}"
    return library.installed_name(appid) or f"app {appid}"


class Dispatch:
    def __init__(self, cfg: dict, log, dry_run: bool = False,
                 on_end_session=None) -> None:
        self.cfg = cfg
        self.voice = cfg["voice"]
        self.log = log
        self.dry_run = dry_run
        # Fires at the top of end_session, while the TV is still on: the room
        # ducker restores here, ~13 s before couch's teardown cuts TV power
        # (2026-08-22). None on lanes with no ducker (the REPL, the bench).
        self.on_end_session = on_end_session
        # See Utterance above; one Dispatch per session.
        self.utterance = Utterance(None, None)

    def begin_utterance(self, turn: str | None, asked: str | None = None) -> None:
        """GrammarGate's one write per final transcript."""
        self.utterance = Utterance(turn, asked)

    # -- internals -------------------------------------------------------------

    def _would(self, what: str) -> Result:
        self.log("dry_run_would", action=what)
        return _ok(f"dry-run: {what}")

    def _exlink(self, what: str, frame_hex: str) -> Result:
        """TV serial send; COM-port contention retry lives in tv.py."""
        if self.dry_run:
            return self._would(f"exlink {what} ({frame_hex})")
        try:
            ack = tv.exlink_send_hex(frame_hex, self.cfg["tvComPort"])
            self.log("exlink_send", cmd=what, ack=ack or "no-ack")
            return _ok(f"exlink {what}")
        except Exception as e:
            self.log.error("exlink_nak", cmd=what, err=str(e))
            return _fail(f"the TV command failed ({what}: {e})")

    # -- session ---------------------------------------------------------------

    def start_session(self, appid: int | str | None = None) -> Result:
        """Advisory busy check; the real arbiter is couch.py's acquire_lock."""
        age = cglib.lock_age()
        if cglib.session_active(age):
            self.log("start_refused", reason="lock_fresh", lock_age_s=round(age))  # type: ignore[arg-type] # active implies aged
            return _busy("a session is already active or starting")
        what = f"couch.py start{f' {appid}' if appid else ''}"
        if self.dry_run:
            return self._would(what)
        args = [sys.executable, str(COUCH), "start"] + ([str(appid)] if appid else [])
        # couch.py runs in its own console, so the turn id travels as an
        # argument; without it couch.py mints its own and only clock time
        # joins the utterance to the launch it caused.
        turn = self.utterance.turn or events.current().get("turn")
        if turn:
            args += ["--turn", turn]
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.log("session_dispatched", appid=appid, turn=turn)
        return _ok(f"starting a session ({what})")

    def end_session(self) -> Result:
        """Works mid-game and mid-launch alike (teardown wins - Exit stops a
        running Enter on the host)."""
        turn = self.utterance.turn            # snapshot at operation start
        # Before the exit, not after the voice session closes: the session
        # stays open for the idle timeout, by which time couch has powered the
        # TV off and remote keys relay nothing (2026-08-22, session fe8302 -
        # 15 points owed). Must not raise: this is a teardown.
        if self.on_end_session is not None:
            try:
                self.on_end_session()
            except Exception as e:
                self.log.warn("end_session_hook_failed", err=repr(e))
        if self.dry_run:
            return self._would("ssh exit")
        # K15-side half of "teardown wins" (2026-08-21): the host Exit only
        # stops an Enter that is RUNNING when it lands, so it loses to
        # couch.py's enter_redispatched rescue; the marker reaches that
        # process, which consumes it at every wait. Written BEFORE the ssh so
        # the K15 side stops even if the exit never gets through.
        cancelled = False
        if cglib.session_active():
            try:
                cglib.CANCEL.write_text(turn or "")
                cancelled = True
            except OSError:
                pass                          # the host-side exit still runs
        try:
            out = gamepc.exit(turn)
        except Exception as e:
            if cancelled:
                # A PC mid-wake is unreachable and has nothing to tear down
                # anyway: the marker alone is the whole teardown.
                self.log("end_session_dispatched", turn=turn, via="cancel")
                return _ok("stopping the launch - the PC wasn't up yet")
            self.log.error("end_session_failed", err=str(e), turn=turn)
            return _fail(f"couldn't reach the PC (ssh exit: {e})")
        if out == "OK":
            self.log("end_session_dispatched", turn=turn)
            return _ok("ending the session")
        self.log.warn("end_session_refused", answer=out, turn=turn)
        return _fail(f"the PC refused the exit (ssh exit: {out})")

    def now_playing(self) -> Result:
        """RunningAppID via the `playing` verb. The one Result whose detail is
        data, not prose: assistant.get_now_playing parses it."""
        if self.dry_run:
            return self._would("ssh playing")
        try:
            out = gamepc.playing().strip()
        except Exception as e:
            return _fail(f"couldn't reach the PC (ssh playing: {e})")
        return _ok(out if out.isdigit() else "0")

    def play_game(self, appid: int | str) -> Result:
        """Session live -> direct host launch (OK/ALREADY/BUSY/NOTREADY).
        No session -> full couch launch, game queued for after READY."""
        if not cglib.session_active():
            return self.start_session(appid)
        if self.dry_run:
            return self._would(f"ssh launch {appid}")
        try:
            # Explicit turn: the assistant path runs in a different task
            # than the gate, so the ambient one is absent (see Utterance).
            out = gamepc.launch(appid, self.utterance.turn)
        except Exception as e:
            self.log.error("launch_failed", appid=appid, err=str(e))
            return _fail(f"couldn't reach the PC (ssh launch: {e})")
        self.log("launch_dispatched", appid=appid, answer=out)
        if out == "OK":
            return _ok(f"launching {_name(appid)}")
        if out == "ALREADY":
            return _ok(f"{_name(appid)} is already running")
        if out.startswith("BUSY:"):
            # Name the blocker: the assistant lane sees only `detail`.
            return _busy(f"{_name(out.split(':', 1)[1])} is already running - "
                         f"it has to be quit first, which I can do if you ask ({out})")
        if out == "NOTREADY":
            # Lock fresh but host pre-READY: a launch is in flight.
            return _busy("the session is still starting")
        if out == "NOTINSTALLED":
            return _fail(f"{_name(appid)} is not installed - "
                         "installing it needs the controller")
        if out.startswith("NOTASK:"):
            return _no_task(out)
        return _fail(f"the launch failed (ssh launch: {out})")

    def quit_game(self, appid: int | str) -> Result:
        """Quit the running game. The host re-checks RunningAppID and answers
        BUSY on a mismatch, so a raced id never kills the wrong game."""
        appid = int(appid)
        if self.dry_run:
            return self._would(f"ssh stop {appid}")
        try:
            out = gamepc.stop(appid, self.utterance.turn)
        except Exception as e:
            self.log.error("quit_failed", appid=appid, err=str(e))
            return _fail(f"couldn't reach the PC (ssh stop: {e})")
        self.log("quit_dispatched", appid=appid, answer=out)
        if out == "OK":
            return _ok(f"quitting {_name(appid)}")
        if out == "NOTRUNNING":
            return _ok("nothing is running to quit")
        if out.startswith("BUSY:"):
            # A different game is up: name it, don't touch it.
            return _busy(f"{_name(out.split(':', 1)[1])} is what's running, not "
                         f"{_name(appid)} - nothing was quit ({out})")
        if out.startswith("NOTASK:"):
            return _no_task(out)
        return _fail(f"the quit failed (ssh stop: {out})")

    # -- Big Picture navigation ------------------------------------------------

    NAV_KINDS = {"downloads", "library", "store", "details", "collection"}

    def nav(self, kind: str, arg: int | str | None = None) -> Result:
        """Fire a steam:// navigation into Big Picture via the host `nav`
        verb. Shared by the assistant tool and the grammar; host-gated on
        the session."""
        kind = str(kind).strip().lower()
        if kind not in self.NAV_KINDS:
            return _fail(f"there's no navigation target called '{kind}'")
        cmd = gamepc.nav_cmd(kind, arg)
        if self.dry_run:
            return self._would(f"ssh {cmd}")
        try:
            out = gamepc.nav(kind, arg, self.utterance.turn)
        except Exception as e:
            self.log.error("nav_failed", kind=kind, err=str(e))
            return _fail(f"couldn't reach the PC (ssh {cmd}: {e})")
        self.log("nav_dispatched", kind=kind, arg=arg, answer=out)
        if out == "OK":
            return _ok(f"showing {self._nav_label(kind, arg)}")
        if out == "NOTREADY":
            # start_session is fire-and-forget (Popen), so "start a session
            # and open X" chains into nav while couch.py is still coming up:
            # a fresh lock means starting, not absent.
            if cglib.session_active():
                return _busy("the session is still starting - "
                             "try again in a moment")
            return _busy("there's no session to navigate - start one first")
        if out.startswith("NOTASK:"):
            return _no_task(out)
        return _fail(f"the navigation failed (ssh {cmd}: {out})")

    def _nav_label(self, kind: str, arg: int | str | None) -> str:
        """Spoken-friendly name for a navigation target."""
        if kind == "details" and arg:
            return _name(arg)
        if kind == "store" and arg:
            return f"{_name(arg)} in the store"
        return {"downloads": "downloads", "library": "your library",
                "store": "the store", "collection": "that collection"}.get(kind, kind)

    # -- TV --------------------------------------------------------------------

    # CAUTION, measured 2026-08-21: with audio on the eARC soundbar the TV acks
    # every Ex-Link volume/mute frame and then refuses it on screen ("Not
    # Available"), so these four verbs move nothing the couch can hear. The
    # write path that works is remote keys over CEC (tv_remote.py).
    def _vol_steps(self, name: str) -> Result:
        step = int(self.voice["volumeStep"])
        if self.dry_run:
            return self._would(f"{name} x{step}")
        for _ in range(step):
            r = self._exlink(name, tv.EXLINK_FRAMES[name])
            if not r.ok:
                return r
            time.sleep(0.05)
        return _ok(f"{name} x{step}")

    def volume_up(self) -> Result:
        return self._vol_steps("vol_up")

    def volume_down(self) -> Result:
        return self._vol_steps("vol_down")

    def volume_set(self, level: int) -> Result:
        """Absolute set, clamped to volumeMax so a misheard number cannot
        blast the room. Also the mute-desync resync."""
        vmax = int(self.voice["volumeMax"])
        clamped = max(0, min(vmax, int(level)))
        if clamped != int(level):
            self.log("volume_clamped", asked=int(level), set=clamped, max=vmax)
        return self._exlink(f"vol_set {clamped}", tv.vol_set_frame(clamped))

    def mute_toggle(self) -> Result:
        """Blind toggle: the S90C has no discrete mute on/off and its status
        query returns a canned echo, byte-identical across volume and mute
        states, so no state is trackable."""
        return self._exlink("mute_toggle", tv.EXLINK_FRAMES["mute_toggle"])

    def switch_input(self, spoken_name: str) -> Result:
        """Config owns the spoken-name -> input map. The GAMING input means
        "get me gaming": no session starts one, mid-launch answers "still
        starting", a READY session flips instantly. Other inputs switch
        freely."""
        cmd = self.voice["inputs"].get(spoken_name.strip().lower())
        if cmd is None:
            return _fail(f"there is no input called '{spoken_name}'")
        if cmd == self.cfg["tvGamingCmd"]:
            if not cglib.session_active():
                # Lock check first: no ssh timeout against a sleeping PC.
                self.log("input_starts_session", input=cmd)
                return self.start_session()
            if self.dry_run:
                return self._would(f"check READY then exlink {cmd}")
            try:
                if gamepc.status() == "NOTREADY":
                    self.log("input_deferred", input=cmd, reason="not_ready")
                    return _busy("the session is still starting - the TV will "
                                 "switch over on its own when it's ready")
            except Exception as e:
                self.log.error("input_refused", input=cmd, err=str(e))
                return _fail(f"couldn't reach the PC (status check: {e})")
        frame_hex = tv.EXLINK_FRAMES.get(cmd)
        if frame_hex is None:
            return _fail(f"that input isn't configured correctly - config maps "
                         f"'{spoken_name}' to unknown command '{cmd}'")
        return self._exlink(f"input {cmd}", frame_hex)
