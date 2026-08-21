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
# by name: one implementation and ONE SEAM, so swapping couch.ssh intercepts
# every verb. `from couch import ssh` would create a second binding that such
# a swap silently misses - i.e. a test patching an unpatched path.

COUCH = cglib.BASE / "couch.py"

Result = namedtuple("Result", "ok earcon detail")

# THE per-utterance context, and the one home for this story. One utterance
# = one immutable snapshot: its correlation id, and the user's words (post
# wake-strip; None on lanes with no transcript - the chord, the REPL).
#
# Handed over EXPLICITLY because a ContextVar cannot do it: one is copied
# into a task at task creation, so the gate setting it never reaches the
# assistant's tool dispatch - a sibling task already running. When this rode
# the ambient copy, voice-driven exits reached the gaming PC uncorrelated.
#
# A namedtuple so the pair swaps atomically: no reader can see one
# utterance's turn beside another's words. The contract it rides on is that
# one utterance is acted on at a time (transcripts reach the gate serially);
# a second one landing mid-dispatch re-points the attribute, so consumers
# snapshot at operation START (test_turn drills that interleave).
Utterance = namedtuple("Utterance", "turn asked")


def _ok(detail, earcon="ok"):
    return Result(True, earcon, detail)


def _busy(detail):
    return Result(False, "busy", detail)


def _fail(detail):
    return Result(False, "fail", detail)


def _no_task(out):
    """NOTASK:<name> - the PC says that scheduled task does not exist. A setup
    step someone skipped, not a fault, so say the fix out loud: the raw
    FAILED:1 this replaced taught nobody anything, and a whole couch test read
    it as "nav is broken" (2026-08-14)."""
    return _fail(f"the {out.split(':', 1)[1]} task isn't registered on the "
                 "gaming PC - it needs the one-time Register-ScheduledTask "
                 "from the setup guide")


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
        # The utterance being acted on (see Utterance above). One Dispatch
        # per session, so this attribute is exactly as isolated as the
        # ContextVar it stands in for.
        self.utterance = Utterance(None, None)

    def begin_utterance(self, turn, asked=None):
        """GrammarGate's one write per final transcript. A method so the
        snapshot's shape has one home."""
        self.utterance = Utterance(turn, asked)

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
        """Advisory busy check - same predicate as the chord, answered
        without spawning a console. The REAL arbiter is couch.py's atomic
        acquire_lock, which owns the whole sequence (and the one rule)."""
        age = cglib.lock_age()
        if cglib.session_active(age):
            self.log("start_refused", reason="lock_fresh", lock_age_s=round(age))
            return _busy("a session is already active or starting")
        what = f"couch.py start{f' {appid}' if appid else ''}"
        if self.dry_run:
            return self._would(what)
        args = [sys.executable, str(COUCH), "start"] + ([str(appid)] if appid else [])
        # couch.py runs in its own console, so the id travels as an argument
        # (a ContextVar survives neither a process nor a task boundary - see
        # Utterance). Without it couch.py mints its own and the user's
        # sentence joins the launch it caused by nothing but a clock reading.
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
        try:
            out = couch.ssh_intent("exit", turn=turn)
        except Exception as e:
            self.log.error("end_session_failed", err=str(e), turn=turn)
            return _fail(f"couldn't reach the PC (ssh exit: {e})")
        if out == "OK":
            self.log("end_session_dispatched", turn=turn)
            return _ok("ending the session")
        self.log.warn("end_session_refused", answer=out, turn=turn)
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
            # Explicit turn (see Utterance): leaning on the ambient one
            # shipped Tier-2 launches uncorrelated, while the Tier-1 path -
            # same task as the gate - quietly worked.
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
            # Name the blocker: the assistant lane sees only `detail`, so a
            # bare appid leaves it saying "something else is running" with no
            # way to say WHAT to quit. The raw code stays for the log. Since
            # quit_game landed, the blocker is now quittable by voice - the
            # message offers it instead of the old "needs the controller".
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
        """Quit the running game on the host. The appid MUST be the one running
        - the host re-checks RunningAppID and refuses (BUSY) otherwise, the same
        truthfulness play_game has, so a raced or wrong id never kills the wrong
        game. This is the voice path out of the "a game left running steals the
        next session" trap that play_game's BUSY message used only to describe."""
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
            # A different game is up: name it, don't touch it. The user asked to
            # quit one thing; quitting another is the wrong-game bug play_game's
            # BUSY exists to avoid.
            return _busy(f"{_name(out.split(':', 1)[1])} is what's running, not "
                         f"{_name(appid)} - nothing was quit ({out})")
        if out.startswith("NOTASK:"):
            return _no_task(out)
        return _fail(f"the quit failed (ssh stop: {out})")

    # -- Big Picture navigation ------------------------------------------------

    NAV_KINDS = {"downloads", "library", "store", "details", "collection"}

    def nav(self, kind, arg=None):
        """Fire a steam:// navigation into Big Picture via the host `nav` verb.
        Low-level and shared: the assistant tool and the grammar both map a
        spoken target to (kind, arg) and call here. Session-gated on the host
        (NOTREADY with no session - nothing to navigate)."""
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
            # and open X" chains into nav while couch.py is still coming up -
            # and "start one first" told the model to start the session it
            # had JUST started, which it relayed as a shrug and the user had
            # to re-ask (2026-08-15). A fresh lock means starting, not absent.
            if cglib.session_active():
                return _busy("the session is still starting - "
                             "try again in a moment")
            return _busy("there's no session to navigate - start one first")
        if out.startswith("NOTASK:"):
            return _no_task(out)
        return _fail(f"the navigation failed (ssh {cmd}: {out})")

    def _nav_label(self, kind, arg):
        """Spoken-friendly name for what we navigated to."""
        if kind == "details" and arg:
            return _name(arg)
        if kind == "store" and arg:
            return f"{_name(arg)} in the store"
        return {"downloads": "downloads", "library": "your library",
                "store": "the store", "collection": "that collection"}.get(kind, kind)

    # -- TV --------------------------------------------------------------------

    # CAUTION, measured 2026-08-21: with sound output on the eARC soundbar,
    # the TV ACKS every Ex-Link volume/mute frame and then refuses it with an
    # on-screen "Not Available" - these four verbs currently move nothing the
    # couch can hear. The write path that works is remote keys relayed over
    # CEC (tv_remote.py; TvDucker below already uses it). Migrating these
    # verbs is deliberately separate work: they are Tier-1 grammar surface,
    # and this file's session ended at the duck.
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
        """Absolute set, clamped to volumeMax - a misheard number must never
        blast the room. Also the mute-desync escape hatch."""
        vmax = int(self.voice["volumeMax"])
        clamped = max(0, min(vmax, int(level)))
        if clamped != int(level):
            self.log("volume_clamped", asked=int(level), set=clamped, max=vmax)
        return self._exlink(f"vol_set {clamped}", cglib.vol_set_frame(clamped))

    def mute_toggle(self):
        """Blind toggle, permanently: the S90C exposes no discrete mute
        on/off, and its status query returns a canned echo that is
        byte-identical across volume and mute states - there is no state to
        read, so none is tracked. 'mute' means toggle; vol_set is the
        resync."""
        return self._exlink("mute_toggle", cglib.EXLINK_FRAMES["mute_toggle"])

    def switch_input(self, spoken_name):
        """Config owns the spoken-name -> input map. The GAMING input means
        "get me gaming": with no session it STARTS one, mid-launch it answers
        "still starting", with a READY session it flips instantly. The one
        rule holds either way - couch.py switches the input only at READY, so
        nothing dead is ever shown. Other inputs switch freely, like a
        remote."""
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


class TvDucker:
    """Drop the room's volume for the length of a voice session; put it back
    on close. One per agent process (the ledger spans sessions), every call
    made from the wake loop's duck() thread under its lock; synchronous.

    Ducking is the one lever that attacks the actual problem - a talker on
    the couch reaches the mic 10-20 dB BELOW dialogue from the TV, and no
    model or threshold recovers a signal that far under. It cannot help the
    wake word (nothing knows to duck until it has fired) but it hands the
    STT a quiet room for every command after it.

    Two incidents shaped this class, one mechanism each:

    - 2026-08-16, duckSteps' first morning: every Ex-Link burst fired at a
      TV that was never on - into standby ahead of a cold launch, into the
      middle of a wake the set was refusing - and the morning ended with the
      only receiver silence on record and an unduck abandoned on its first
      frame. Hence THE GATE: duck() asks the TV whether it is ON first
      (cglib.tv_power_state answers from standby); anything else, unknown
      included, means skip. Skipping is always safe - the whole cost is one
      session of loud TV.
    - 2026-08-21, the eARC discovery: with audio on the soundbar the TV
      ACKS every direct volume write and refuses it on screen, so send-and-
      hope is not just fragile, it can be theater. Hence THE READBACK:
      writes are remote keys relayed over CEC (tv_remote.press - the one
      thing the eARC path honours, benched same day), and the TV's
      pairing-free UPnP volume (cglib.tv_volume - it mirrors the BAR's
      level) is ground truth for what actually happened. The ledger holds
      only VERIFIED movement, so restore restores exactly what moved, a
      shortfall carries as debt the next session's close pays off, and a
      human working the remote mid-session is DETECTED (the readback is not
      where we left it) - the duck stands down rather than stomp their
      choice.

    The ledger dies with the process; that gap (restart between duck and
    unduck) is documented at the caller and unchanged."""

    TOPUPS = 2            # extra key rounds when the readback comes up short
    POLLS = 6             # readback polls per settle, POLL_GAP_S apart
    POLL_GAP_S = 0.4

    def __init__(self, steps, tv_ip, log, dry_run=False, to_pct=None,
                 probe=None, read=None, press=None, pause=time.sleep):
        # to_pct (1-99) wins over steps: duck TO that percent of the
        # pre-duck level, so the drop scales with how loud the room
        # actually is. Only expressible at all because the readback exists
        # - a blind channel cannot take a fraction of a level it cannot
        # read, which is why the Ex-Link design never had this knob.
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
        Returns the last level actually SEEN - `now` if nothing verified,
        which is the honest answer when keys or readback die mid-drive: a
        press that raises still counts for nothing until the readback moves.
        Bounded rounds, so a dead relay gets a couple of bursts, never a
        storm (the 08-16 lesson, kept)."""
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
        """Restore everything on the ledger - this session's duck plus any
        debt an earlier shortfall left behind. Quiet when the ledger is
        empty (a skipped duck owes nothing)."""
        if not self.out:
            return
        want = self.out
        if self.dry_run:
            self.log("dry_run_would", action=f"unduck +{want}")
            self.out, self.expect = 0, None
            return
        now = self.read()
        if now is None:
            # TV gone (off, DMR asleep with the panel): keys would not
            # relay anyway. Keep the debt; the next close retries.
            self.log("tv_unducked", steps=0, asked=want, ok=False,
                     reason="no_readback")
            self.log.warn("tv_duck_deficit", steps=self.out)
            return
        if self.expect is not None and now != self.expect:
            # A human moved the volume mid-session. They have chosen a
            # level; adding our delta back lands ABOVE their choice. Only
            # the readback makes this detectable - stand down, owe nothing.
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
