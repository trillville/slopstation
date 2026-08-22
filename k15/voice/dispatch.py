"""Every voice-triggered side effect: GrammarGate (Tier 1) and the
assistant's tools (Tier 2) call the same functions.

Actions return a Result: `earcon` names the count-coded tone, `detail` is
both the log line and the only thing the assistant lane reports to the model
- prose a stranger could act on, not a status code.

dry_run=True logs intent instead of acting; the lock check stays live.
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
# by name: `from couch import ssh` makes a second binding that patching
# couch.ssh would miss.

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


def _ok(detail, earcon="ok"):
    return Result(True, earcon, detail)


def _busy(detail):
    return Result(False, "busy", detail)


def _fail(detail):
    return Result(False, "fail", detail)


def _no_task(out):
    """NOTASK:<name> - a scheduled task the PC never had registered."""
    return _fail(f"the {out.split(':', 1)[1]} task isn't registered on the "
                 "gaming PC - it needs the one-time Register-ScheduledTask "
                 "from the setup guide")


def _name(appid):
    """appid -> installed title, falling back to the bare id. Never raises:
    the index is a cache (empty on a fresh K15, stale after an install)."""
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
        # See Utterance above; one Dispatch per session.
        self.utterance = Utterance(None, None)

    def begin_utterance(self, turn, asked=None):
        """GrammarGate's one write per final transcript."""
        self.utterance = Utterance(turn, asked)

    # -- internals -------------------------------------------------------------

    def _would(self, what):
        self.log("dry_run_would", action=what)
        return _ok(f"dry-run: {what}")

    def _exlink(self, what, frame_hex):
        """TV serial send; COM-port contention retry lives in cglib."""
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
        """Advisory busy check; the real arbiter is couch.py's acquire_lock."""
        age = cglib.lock_age()
        if cglib.session_active(age):
            self.log("start_refused", reason="lock_fresh", lock_age_s=round(age))
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

    def end_session(self):
        """Works mid-game and mid-launch alike (teardown wins - Exit stops a
        running Enter on the host)."""
        turn = self.utterance.turn            # snapshot at operation start
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
            out = couch.ssh_intent("exit", turn=turn)
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

    def now_playing(self):
        """RunningAppID via the `playing` verb. The one Result whose detail is
        data, not prose: assistant.get_now_playing parses it."""
        if self.dry_run:
            return self._would("ssh playing")
        try:
            out = couch.ssh("playing").strip()
        except Exception as e:
            return _fail(f"couldn't reach the PC (ssh playing: {e})")
        return _ok(out if out.isdigit() else "0")

    def play_game(self, appid):
        """Session live -> direct host launch (OK/ALREADY/BUSY/NOTREADY).
        No session -> full couch launch, game queued for after READY."""
        if not cglib.session_active():
            return self.start_session(appid)
        if self.dry_run:
            return self._would(f"ssh launch {appid}")
        try:
            # Explicit turn: the Tier-2 path runs in a different task than
            # the gate, so the ambient one is absent (see Utterance).
            out = couch.ssh_intent(f"launch {appid}", turn=self.utterance.turn)
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

    def quit_game(self, appid):
        """Quit the running game. The host re-checks RunningAppID and answers
        BUSY on a mismatch, so a raced id never kills the wrong game."""
        appid = int(appid)
        if self.dry_run:
            return self._would(f"ssh stop {appid}")
        try:
            out = couch.ssh_intent(f"stop {appid}", turn=self.utterance.turn)
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

    def nav(self, kind, arg=None):
        """Fire a steam:// navigation into Big Picture via the host `nav`
        verb. Shared by the assistant tool and the grammar; host-gated on
        the session."""
        kind = str(kind).strip().lower()
        if kind not in self.NAV_KINDS:
            return _fail(f"there's no navigation target called '{kind}'")
        cmd = f"nav {kind}" + (f" {arg}" if arg not in (None, "") else "")
        if self.dry_run:
            return self._would(f"ssh {cmd}")
        try:
            out = couch.ssh_intent(cmd, turn=self.utterance.turn)
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

    def _nav_label(self, kind, arg):
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
    def _vol_steps(self, name, steps=None):
        step = int(self.voice["volumeStep"]) if steps is None else int(steps)
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
        """Absolute set, clamped to volumeMax so a misheard number cannot
        blast the room. Also the mute-desync resync."""
        vmax = int(self.voice["volumeMax"])
        clamped = max(0, min(vmax, int(level)))
        if clamped != int(level):
            self.log("volume_clamped", asked=int(level), set=clamped, max=vmax)
        return self._exlink(f"vol_set {clamped}", cglib.vol_set_frame(clamped))

    def mute_toggle(self):
        """Blind toggle: the S90C has no discrete mute on/off and its status
        query returns a canned echo, byte-identical across volume and mute
        states, so no state is trackable."""
        return self._exlink("mute_toggle", cglib.EXLINK_FRAMES["mute_toggle"])

    def switch_input(self, spoken_name):
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


class TvDucker:
    """Drop the room's volume for a voice session; restore it on close. A
    talker on the couch reaches the mic 10-20 dB below TV dialogue.

    One per agent process, synchronous, every call from the wake loop's
    duck() thread under its lock. The ledger spans sessions but dies with the
    process, so a restart between duck and unduck loses it.

    Gate: duck() runs only when cglib.tv_power_state says "on" (it answers
    from standby too); anything else, unknown included, skips (2026-08-16).

    Readback: writes are remote keys over CEC (tv_remote.press, the only path
    the eARC setup honours) and the pairing-free UPnP volume (cglib.tv_volume,
    mirroring the soundbar) is ground truth. The ledger holds only VERIFIED
    movement, so a shortfall carries as debt to the next close and a human
    moving the remote mid-session is detected (2026-08-21)."""

    TOPUPS = 2            # extra key rounds when the readback comes up short
    POLLS = 6             # readback polls per settle, POLL_GAP_S apart
    POLL_GAP_S = 0.4

    def __init__(self, steps, tv_ip, log, dry_run=False, to_pct=None,
                 probe=None, read=None, press=None, pause=time.sleep):
        # to_pct (1-99) wins over steps: duck TO that percent of the pre-duck
        # level, so the drop scales with how loud the room is.
        self.steps = int(steps)
        self.to_pct = int(to_pct) if to_pct else None
        self.log = log
        self.dry_run = dry_run
        self.probe = probe or (lambda: cglib.tv_power_state(tv_ip))
        self.read = read or (lambda: cglib.tv_volume(tv_ip))
        self.press = press or self._ws_press(tv_ip)
        self.pause = pause
        self.out = 0        # verified steps down, not yet restored
        self.expect = None  # the readback our last op left behind

    @staticmethod
    def _ws_press(tv_ip):
        def go(direction, n):
            import tv_remote            # lazy: samsungtvws lives in the venv
            tv_remote.TvRemote(tv_ip).press(direction, n)
        return go

    def _settle(self, target):
        """Poll the readback toward target; the relay lands in ~1 s."""
        for _ in range(self.POLLS):
            v = self.read()
            if v == target:
                return v
            self.pause(self.POLL_GAP_S)
        return self.read()

    def _drive(self, direction, now, target):
        """Press toward target, verify by readback, top up what got lost.
        Returns the last level actually SEEN - `now` if nothing verified.
        Rounds are bounded so a dead relay gets bursts, not a storm."""
        best = now
        for _ in range(1 + self.TOPUPS):
            need = abs(target - best)
            if not need:
                break
            try:
                self.press(direction, need)
            except Exception as e:
                self.log.warn("tv_duck_failed", stage="press", err=str(e))
            final = self._settle(target)
            if final is None:
                break
            if final == best:
                break                   # keys verifiably bought nothing: stop
            best = final
        return best

    def duck(self):
        state = self.probe()
        if state != "on":
            self.log("tv_duck_skipped", state=state or "unknown",
                     debt=self.out)
            return
        v0 = self.read()
        if v0 is None:
            self.log("tv_duck_skipped", state="on", reason="no_readback",
                     debt=self.out)
            return
        if self.to_pct:
            target = min(v0, round(v0 * self.to_pct / 100))
            asked = v0 - target        # scales with v0; never clamps below 0
        else:
            target = max(0, v0 - self.steps)
            asked = self.steps         # flat; the 0-clamp shows as landed<asked
        if self.dry_run:
            self.log("dry_run_would", action=f"duck vol {v0}->{target}")
            self.out += v0 - target
            self.expect = target
            return
        final = self._drive("down", v0, target)
        landed = max(0, v0 - final)
        self.out += landed
        self.expect = final
        self.log("tv_ducked", steps=landed, asked=asked, vol=final,
                 ok=final == target)

    def unduck(self):
        """Restore the ledger: this session's duck plus any earlier debt."""
        if not self.out:
            return
        want = self.out
        if self.dry_run:
            self.log("dry_run_would", action=f"unduck +{want}")
            self.out, self.expect = 0, None
            return
        now = self.read()
        if now is None:
            # TV gone (off, DMR asleep with the panel): keys would not relay.
            # Keep the debt; the next close retries.
            self.log("tv_unducked", steps=0, asked=want, ok=False,
                     reason="no_readback")
            self.log.warn("tv_duck_deficit", steps=self.out)
            return
        if self.expect is not None and now != self.expect:
            # A human moved the volume mid-session: adding our delta back
            # would land above their chosen level. Stand down.
            self.log("tv_unducked", steps=0, asked=want, ok=True,
                     reason="user_adjusted", vol=now)
            self.out, self.expect = 0, None
            return
        target = min(100, now + want)
        final = self._drive("up", now, target)
        restored = max(0, final - now)
        self.out = max(0, self.out - restored)
        self.expect = final if self.out else None
        self.log("tv_unducked", steps=restored, asked=want, vol=final,
                 ok=self.out == 0)
        if self.out:
            self.log.warn("tv_duck_deficit", steps=self.out)
