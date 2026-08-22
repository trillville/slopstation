"""K15 orchestrator: Ex-Link TV power -> WoL -> ssh enter -> poll READY ->
switch TV input -> watch loop. The one rule: nothing switches the TV to the
gaming input before the host writes READY, so every pre-READY failure leaves
the TV exactly as the viewer had it."""
import os, socket, subprocess, sys, time

import cglib
import events

CFG  = cglib.load_config()

PORT_WAIT_S    = 90    # PC power-on/resume until sshd answers
ENTER_ATTEMPTS = 60    # ~1/s; also covers waiting out logon after a cold boot
READY_WAIT_S   = 120   # Enter dispatch until the READY marker appears
WAKE_RETRY_S   = 30    # this far into the READY wait, re-send power_on once
                       # (see the send in start()) - the TV-asleep rescue the
                       # gaming PC structurally cannot perform. Past the healthy
                       # envelope on purpose: launches reach READY in ~9-20 s, so
                       # at 10 s the frame fired on most of them and `again` gave
                       # a count of slow launches instead of stuck ones.
ENTER_REDISPATCH = 1   # extra Enter dispatches after a DETECTED death (not a
                       # timeout). Every launch that ever reached READY did so
                       # in 9-20 s, while all three recorded failures burned the
                       # full READY_WAIT_S waiting on an Enter that had already
                       # exited - 2026-08-13 17:20, 08-16 17:56, 08-19 01:18,
                       # every one of them a TV that acked power_on and stayed
                       # dark. One re-dispatch is the whole rescue: the set that
                       # refuses the first wake usually takes a later one, and
                       # Enter's abort path leaves OFFICE topology behind, which
                       # is exactly the clean state a fresh Enter wants.
                       #
                       # The budget this buys, worst case: 08-19's Enter died at
                       # 48.3 s (gamepc enter_failed, turn 7402df) and the K15
                       # then waited another 71 s on a task already gone. So
                       # detection lands ~50 s in, the retry gets a fresh
                       # READY_WAIT_S, and a launch that is going to fail twice
                       # takes ~170 s instead of 121 s. That is the deliberate
                       # trade - a minute longer on the launches that were
                       # already lost, for the ones that just needed asking
                       # twice.
ENTER_SETTLE_S = 25    # how long a NOTREADY stays UNremarkable. Two jobs: it
                       # outlasts the gap between schtasks /Run returning (the
                       # task is only TRIGGERED) and the task actually reading
                       # as running, and it sits past the slowest launch that
                       # ever worked (19.8 s), so a HEALTHY launch never spends
                       # a single extra ssh round-trip asking about Enter.
WATCH_POLL_S   = 5
WATCH_FAILS    = 3     # consecutive ssh failures (raised, see ssh()) = session
                       # dead. Deliberately low: a true sleep restores the TV in
                       # ~20-30s; the false-positive cost of a transient outage
                       # is a desk-side relaunch (the Puck stays claimed, so the
                       # chord can't hear).
TV_WAIT_S      = 30    # power_on until the set REPORTS "on" (wait_tv_on in
                       # start()), before Enter is dispatched. The state flips
                       # ~5 s after a frame the set accepts (measured
                       # 2026-08-19: standby at t+1..4 s, on at t+5 s), so 30 s
                       # covers the flip plus several re-pokes - and a set
                       # still not on at 30 s is refusing, which is worth a
                       # failure that NAMES it: every recorded TV-refusal
                       # burned 47-121 s learning the same thing from Enter.
TV_POKE_S      = 6     # re-send power_on this often while the set is not on:
                       # just past the ~5 s flip lag, so each frame gets its
                       # answer before the next. Discrete power_on - safe to
                       # repeat (see the READY-wait re-poke for the full
                       # argument).
TV_UNKNOWN_N   = 5     # consecutive unreadable answers before the gate stands
                       # down to the legacy blind path. None is UNKNOWN, never
                       # "off" (Wi-Fi blip, IP drift, a rig with no tvIp) - a
                       # read that cannot answer must never cost a launch that
                       # would have worked.

log = cglib.make_log("launch")


class Cancelled(BaseException):
    """A requested stop of an in-flight launch - the K15-side half of
    "teardown wins". The voice lane's end_session can only ssh `exit`, and
    Exit can only stop an Enter that is RUNNING when it lands: on 2026-08-21
    (turn 0b785e) the first Enter had already died, so the exit raced this
    process's enter_redispatched rescue and won by six seconds of luck -
    the reverse ordering drops OFFICE topology and a released Puck onto a
    live couch session. end_session now also writes cglib.CANCEL, and every
    wait in start() consumes it through raise_if_cancelled().

    BaseException ON PURPOSE, exactly like KeyboardInterrupt: it must ride
    the abort handler (launch_aborted, lock released, no last_error, no fail
    buzz - whoever cancelled already knows), never the launch_failed path."""

    def __init__(self, by):
        self.by = by                    # the CANCELLING intent's turn, or ""
        super().__init__(by)


def raise_if_cancelled():
    """Consume a pending cancel (unlink is the ack - a marker left behind
    would kill the next launch too) and stop the launch through the abort
    path. The unlink-then-raise order is safe against a crash between them:
    a re-raise never happens, and a consumed-but-unraised marker costs one
    ssh poll, not a launch."""
    try:
        by = cglib.CANCEL.read_text().strip()
    except OSError:
        return                          # no marker - the overwhelming case
    cglib.CANCEL.unlink(missing_ok=True)
    raise Cancelled(by)


def exlink(name, **fields):
    try:
        ack = cglib.exlink_send(name, CFG["tvComPort"])
        # `ack` means the TV's serial receiver ACCEPTED THE FRAME. It is not
        # confirmation that the TV acted on it, and there is no read-back that
        # would be: Ex-Link here is send-only. A power_on can ack and leave the
        # set dark - 2026-08-13, where reading this field as "the TV came on"
        # sent the investigation at the gaming PC for hours.
        log("exlink_send", cmd=name, ack=ack or "no-ack", **fields)
    except Exception as e:
        # non-fatal: PC readiness is independent of whether the TV heard us
        log.error("exlink_nak", cmd=name, err=str(e), **fields)


def wol():
    mac = bytes.fromhex(CFG["gamingPcMac"].replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
    log("wol_sent")


def ssh(cmd, timeout=15):
    """Run one Dispatch verb on the host; returns its stdout.

    check=True is load-bearing: an unreachable host RAISES instead of returning
    ssh's error text. Without it, connection errors read as session state -
    start()'s READY poll would treat 'ssh: connect ... timed out' as READY and
    switch the TV to a dead input, and watch() could never detect sleep (its
    fails counter only moves on exceptions). stdout-only keeps stderr noise out
    of state comparisons; Dispatch reports its own failures as FAILED:<code>."""
    r = subprocess.run(["ssh", CFG["sshHost"], cmd],
                       capture_output=True, text=True, timeout=timeout, check=True)
    return r.stdout.strip()


def ssh_intent(cmd, turn=None, **kw):
    """A MUTATING verb, tagged with this launch's turn id so the gaming PC's
    transcript and events join the same story as ours. Read-only polls
    (status/games/playing) go through plain ssh(): they are not intents, and
    tagging them would multiply the id across noise.

    Pass `turn` explicitly from any caller whose ambient context predates the
    utterance - the voice lane's, which a ContextVar cannot reach (see
    dispatch.Utterance). Ambient is the default for couch.py's own in-process
    calls, where it does propagate."""
    turn = turn or events.current().get("turn")
    # Re-validate at the wire: Dispatch fails CLOSED on a malformed id (it
    # matches no verb and answers DENIED), so a telemetry bug must not be
    # able to take launches down with it. Uncorrelated beats refused.
    return ssh(f"{cmd} --turn {turn}" if events.valid_turn(turn) else cmd, **kw)


def enter_running():
    """True/False if the gaming PC could tell us whether its Enter task is
    still running; None if it could not.

    Windows' own task state is the authority - deliberately NOT a marker file,
    which would be one more piece of distributed state owing a reconciler
    (README § Code architecture). There is nothing here to leave behind.

    The None is the load-bearing part. A PC deployed before the `enterstate`
    verb existed answers DENIED, and an ssh blip raises; both mean "no
    information", which must never read as "Enter is dead" - a launch that
    re-dispatched on a blip would fight a healthy Enter. Only an explicit
    False moves anything, and the fallback for everything else is the
    READY_WAIT_S timeout that has always been here."""
    try:
        ans = ssh("enterstate")
    except Exception:
        return None
    if ans == "RUNNING":
        return True
    # NOTASK is unreachable in practice (dispatch_enter would never have got
    # its OK) but it is still definitely-not-running, so say so.
    if ans in ("IDLE", "NOTASK"):
        return False
    return None


def wait_port(timeout=PORT_WAIT_S):
    end = time.time() + timeout
    while time.time() < end:
        # A cold boot spends up to the whole 90 s here - "end the session"
        # during it must not wait that out.
        raise_if_cancelled()
        try:
            with socket.create_connection((CFG["gamingPcIp"], 22), 3):
                return True
        except OSError:
            time.sleep(1)
    return False


def start(appid=None, turn=None):
    # One id for one intent: the voice agent or the listener mints it so the
    # whole chain - wake, dispatch, this launch, the PC's Enter task - shares
    # one story. A direct run (Start-TV-Gaming.bat, a bench invocation) mints
    # its own, so nothing is ever uncorrelated.
    turn = turn if events.valid_turn(turn) else events.new_turn()
    events.context(turn=turn)
    # The pre-read only shapes the log lines - acquire_lock is the arbiter,
    # and losing it means another launch won in the last few milliseconds.
    age = cglib.lock_age()
    if cglib.session_active(age):
        log("launch_busy", lock_age_s=round(age)); return 1
    if age is not None:
        log.warn("lock_recycled", lock_age_s=round(age))
    if not cglib.acquire_lock(f"{turn} {os.getpid()}"):
        log("launch_busy", reason="lost_acquire_race"); return 1
    # A cancel that predates this intent is void - it was aimed at whatever
    # the rig was doing BEFORE this launch existed, and honouring it here
    # would kill a launch nobody asked to stop.
    cglib.CANCEL.unlink(missing_ok=True)
    t0 = time.time()

    def ms():
        """Every milestone carries its distance from the chord/voice trigger,
        so time-to-READY is a distribution in Grafana rather than a number
        someone once measured by hand."""
        return round((time.time() - t0) * 1000)

    try:
        tv_ip = CFG.get("tvIp")
        # The raw depth rung as the launch FOUND the set - "on", "standby"
        # (shallow), "" (deep: hours off, the IP server still answering in
        # 3 ms with PowerState drained) or null (unreachable). Logged for
        # the open correlation: does "" at launch_start predict the
        # acked-and-refused wake? The Ex-Link ack can never carry this - it
        # is a CONSTANT, probed 2026-08-19: the same 030cf1 whatever the
        # power state and whatever the command, nothing past three bytes at
        # all. Do not re-run that probe, and do not brute-force the command
        # space hunting a status frame - the same protocol carries
        # service-mode commands, and a valid-checksum guess is not a safe
        # thing to fire at a TV someone watches.
        tv0 = cglib.tv_power_state(tv_ip, timeout=1.0, raw=True) if tv_ip else None
        log("launch_start", appid=appid, **({"tv": tv0} if tv_ip else {}))
        exlink("power_on")
        wol()
        if not wait_port(): raise RuntimeError("gaming PC never became reachable")
        log("ssh_up", dur_ms=ms())

        def wait_tv_on():
            """The gate the launch path was guessing without: power_on ->
            poll PowerState until "on" -> only THEN dispatch Enter. Every
            recorded TV refusal (2026-08-13 17:20, 08-16 17:56, 08-19 01:18,
            and 0b785e on 08-21) was a set that acked power_on, stayed dark,
            and let Enter burn 47-121 s discovering a display that was never
            there - the receiver is powered in standby, so the ack proves
            delivery and nothing else, and the gaming PC cannot see the
            panel either (EDID and all three WMI monitor classes read
            identically awake or asleep across a full power cycle,
            2026-08-13). Idle duration does not predict the refusal:
            failures at 11.8-20.0 h since last session, successes at 22+ h.
            The enter_died re-dispatch remains as the rescue for rigs this
            gate cannot serve - it converts a refused first wake into a
            slower success whenever a later frame lands; this gate spends
            the budget on the thing that actually has to happen and makes
            that rescue rare rather than the primary signal.

            Fail-open on silence: a set that cannot be READ (no tvIp, IP
            drift, Wi-Fi blip - the endpoint rides Wi-Fi) gets the legacy
            blind path, because an unreadable TV is not a refused one.
            Fail-closed only on the set's own word: TV_WAIT_S of answered
            "standby"/"" is the set refusing, reported while whoever asked
            is still holding the controller."""
            if not tv_ip:
                return
            give_up = time.time() + TV_WAIT_S
            poke_at = time.time() + TV_POKE_S
            unknowns = 0
            state = tv0
            while time.time() < give_up:
                cglib.touch_lock()
                raise_if_cancelled()
                state = cglib.tv_power_state(tv_ip, timeout=1.0, raw=True)
                if state == "on":
                    # dur_ms here is the frame-to-lit distribution - the
                    # number TV_WAIT_S and TV_POKE_S are tuned against.
                    log("tv_on", dur_ms=ms())
                    return
                if state is None:
                    unknowns += 1
                    if unknowns >= TV_UNKNOWN_N:
                        log.warn("tv_state_unknown", dur_ms=ms())
                        return
                else:
                    unknowns = 0
                if time.time() >= poke_at:
                    exlink("power_on", again=True)
                    poke_at = time.time() + TV_POKE_S
                time.sleep(1)
            raise RuntimeError(f"TV never reported on (PowerState={state!r} "
                               f"after {TV_WAIT_S}s of asking) - the set is "
                               "refusing the wake, not missing the frame")

        wait_tv_on()

        def dispatch_enter(event, attempts=ENTER_ATTEMPTS):
            """Trigger the Enter task; True once Dispatch answered OK. Called
            again on the re-dispatch path, which is why it is a function.

            `attempts` is per-call because the two callers want opposite
            things: the first dispatch spends ENTER_ATTEMPTS partly to wait out
            logon after a cold boot, while a re-dispatch already knows the PC
            is up and answering - burning a minute there would spend the whole
            READY window on retries of a call that is not the problem."""
            for _ in range(attempts):
                cglib.touch_lock()
                raise_if_cancelled()
                try:
                    if ssh_intent("enter") == "OK":
                        log(event, dur_ms=ms()); return True
                except Exception as e:
                    log.warn("enter_retry", err=str(e))
                time.sleep(1)
            return False

        if not dispatch_enter("enter_dispatched"):
            raise RuntimeError("could not trigger Enter task")
        end = time.time() + READY_WAIT_S
        ready = False
        foreign_seen = None
        redispatches = ENTER_REDISPATCH
        idle_seen = 0
        settle_at = time.time() + ENTER_SETTLE_S
        repoke_at = time.time() + WAKE_RETRY_S
        while time.time() < end:
            cglib.touch_lock()
            raise_if_cancelled()
            # The only rescue there is for a TV that slept through the power_on
            # at launch_start. Enter's profile retry re-applies TV-GAMING but
            # cannot ask the set to wake - the gaming PC has no Ex-Link, this
            # process does - so a sleeping TV used to cost the whole 120 s READY
            # wait and the launch with it (2026-08-13 10:20). WAKE_RETRY_S puts
            # the frame after Enter's first profile check has failed and before
            # its retry apply, which is the only window where a set that wakes
            # now still rescues this launch: Enter runs to ~66 s (20 s check,
            # then an OFFICE restore and a retry at up to 20 s each).
            #
            # It used to be true that "once it dies nothing re-runs it, so a
            # later wake buys nothing" - that is what ENTER_REDISPATCH below
            # changed, and it is why this frame is no longer the only rescue.
            # The blind poke stays because it is free and it is the ONLY thing
            # that can help a set which wakes while Enter is still running.
            #
            # Safe to repeat: EXLINK_FRAMES holds DISCRETE power_on/power_off
            # values, so this is a no-op on a set already on - mute is the only
            # toggle in that table. It is also not an input switch, so the one
            # rule is untouched, and it stays inside the pre-READY window where
            # a failure still leaves the TV exactly as the viewer had it.
            #
            # `again=True` is the field to count after deploying, and it claims
            # exactly what every other Ex-Link line claims: that we asked twice.
            # Nothing here can say the set heard either frame.
            if repoke_at and time.time() >= repoke_at:
                exlink("power_on", again=True)
                repoke_at = None
            try:
                st = ssh("status")
                # Generation identity: the marker echoes the turn Enter was
                # given, so a READY can be matched to the launch that caused
                # it. Our turn = this launch's ready.
                if st == turn:
                    log("host_ready", status=st, dur_ms=ms(), verified=True)
                    ready = True; break
                if st != "NOTREADY":
                    if events.valid_turn(st):
                        # Someone else's turn: a stale marker. Keep waiting -
                        # our Enter overwrites it, so this converges, where
                        # "any non-NOTREADY is ready" used to switch the TV
                        # to a host still mid-Enter. The rare inverse (the
                        # turn file overwritten before Enter read it) also
                        # lands here, failing closed into a clean retry.
                        if st != foreign_seen:
                            log.warn("ready_foreign", status=st)
                            foreign_seen = st
                    else:
                        # ISO timestamp: a PC deployed before turn-stamping,
                        # or a turnless Enter. Accept (either side may deploy
                        # first) but record that it was unverified.
                        log("host_ready", status=st, dur_ms=ms(),
                            verified=False)
                        ready = True; break
            except Exception as e:
                log.warn("status_poll_failed", err=str(e))
                time.sleep(1); continue
            # Still NOTREADY. An Enter that is no longer running will never
            # write the marker, so the rest of the READY window is dead time -
            # 121 s of it on each of the three recorded failures, every second
            # of which someone spent on the couch watching a dark TV. Re-poke
            # and re-dispatch instead: the TV gets another chance to wake, and
            # Enter gets another chance to see it.
            if (st == "NOTREADY" and time.time() >= settle_at
                    and enter_running() is False):
                # Enter writes the marker and THEN exits, so a single
                # (NOTREADY, task-idle) pair can be those two instants read in
                # the wrong order. Demand it twice: the next poll re-reads
                # status, so a marker that landed in between is picked up by
                # the normal path above and this never fires on a launch that
                # had just succeeded - re-dispatching onto a live session
                # would tear down the very thing we were waiting for.
                idle_seen += 1
                if idle_seen >= 2:
                    log.warn("enter_died", dur_ms=ms())
                    # THE race this marker exists for, re-checked at the last
                    # instant: a cancel that lands while the death was being
                    # proven must win here, before the re-poke and the second
                    # Enter. The loop-top check alone leaves a window exactly
                    # one iteration wide, and 0b785e's exit fell inside it.
                    # (A cancel arriving AFTER the redispatch is the host's
                    # to resolve: Exit stops a running Enter.)
                    raise_if_cancelled()
                    # Out of retries: say so NOW. Sitting out the rest of the
                    # window on a task we have just proved is gone is exactly
                    # the dead time this whole change exists to delete - and
                    # the fail buzz should reach the couch while whoever
                    # pressed the chord is still holding the controller.
                    if not redispatches:
                        raise RuntimeError("Enter exited without READY")
                    exlink("power_on", again=True)
                    if not dispatch_enter("enter_redispatched", attempts=5):
                        raise RuntimeError("Enter died and could not be re-triggered")
                    redispatches -= 1
                    idle_seen = 0
                    settle_at = time.time() + ENTER_SETTLE_S
                    # The retry gets a full window of its own. Without this it
                    # would inherit whatever seconds the first Enter had not
                    # already spent, which on the failures this targets is
                    # nowhere near enough to finish - a rescue that cannot
                    # reach READY is just a slower way to fail.
                    end = time.time() + READY_WAIT_S
            else:
                idle_seen = 0
            time.sleep(1)
        if not ready: raise RuntimeError("host never reported READY")
        cglib.LAST_ERROR.unlink(missing_ok=True)   # success supersedes any old failure
        exlink(CFG["tvGamingCmd"])
        if appid:
            # Voice "play <title>" from cold: queue the game once the session
            # is READY. Best-effort - a failed game launch never fails the
            # session; Big Picture being up is a working outcome on its own.
            #
            # Except on ALREADY, which means the PC found this appid running
            # from an EARLIER session - the one shape where landing on Big
            # Picture is not a working outcome. Big Picture comes up and the
            # couch cannot drive it (see the ready event's comment in
            # Enter-TV.ps1 for what that looks like and what has been ruled
            # out). Warn so the launch reads as degraded: 2026-08-13 turn
            # 14852d looked flawless from here and was unusable on the couch.
            try:
                answer = ssh_intent(f"launch {appid}")
                emit = log.warn if answer == "ALREADY" else log
                emit("game_launch", appid=appid, result=answer)
            except Exception as e:
                log.warn("game_launch_failed", appid=appid, err=str(e))
        log("session_gaming", dur_ms=ms()); watch(expected=turn)
    except Exception as e:
        log.error("launch_failed", err=str(e), dur_ms=ms())
        try:
            # The listener polls this and signals the failure haptically (3 thuds).
            cglib.LAST_ERROR.write_text(str(e))
        except OSError:
            pass
        cglib.release_lock(); return 1
    except BaseException as e:
        # Ctrl-C in the launch console is a KeyboardInterrupt, which is NOT an
        # Exception - so the handler above never saw it and this lane had one
        # silent death: 2026-08-16 turn b43b74 dispatched Enter and then
        # emitted nothing at all. No terminal event, no fail buzz, and a lock
        # released only later by staleness recycling. The cost was a launch
        # that looked, in Grafana, like it was still running forever.
        #
        # Deliberately no LAST_ERROR: whoever pressed Ctrl-C already knows,
        # and buzzing the Puck three times to tell them would be noise. The
        # lock still has to go, which is the whole reason this clause exists.
        #
        # Cancelled arrives here BY DESIGN (see the class): a voice "end the
        # session" against an in-flight launch is the same intent as Ctrl-C,
        # from the couch instead of the desk. cancelled_by joins the two
        # stories - this launch's turn and the utterance that stopped it.
        by = {"cancelled_by": e.by} if isinstance(e, Cancelled) and e.by else {}
        log.warn("launch_aborted", err=type(e).__name__, dur_ms=ms(), **by)
        cglib.release_lock(); return 1
    return 0


def watch(expected=None):
    """Poll the session until it ends, then restore the TV and release the
    lock. `expected` is the turn the marker should keep echoing: a different
    one means a successor owns the rig, so leave the TV and lock to it. None
    disables the check (a reconcile that adopted a legacy marker)."""
    fails = 0
    died_by_fails = False
    while True:
        time.sleep(WATCH_POLL_S)
        cglib.touch_lock()
        try:
            st = ssh("status"); fails = 0
            if st == "NOTREADY":
                log("session_ended", reason="host"); break
            if expected and events.valid_turn(st) and st != expected:
                log.warn("session_ended", reason="superseded", status=st)
                return
        except Exception:
            fails += 1
            if fails >= WATCH_FAILS:
                log.warn("session_ended", reason="ssh_fails", fails=fails)
                died_by_fails = True
                break
    if died_by_fails:
        # A fails-death can leave a live PC holding the Puck in TV topology -
        # and a held Puck means a deaf chord, so recovery would need the desk.
        # Best-effort exit dispatch: if the PC is actually alive behind a
        # transient blip, its teardown restores the desk and sends the Puck
        # home; if it's truly asleep, this raises and nothing changes.
        try:
            if ssh_intent("exit") == "OK":
                log("exit_dispatched", reason="release_puck_after_ssh_fails")
        except Exception:
            pass
    exlink("power_off" if CFG["tvOffWhenDone"] else CFG["tvIdleCmd"])
    # Ownership-checked: a lock recycled while we stalled belongs to the
    # successor, and unlinking it would free a live session.
    if not cglib.release_lock():
        log.warn("lock_kept", reason="owned_by_successor")
    log("session_idle")


def reconcile():
    """Run once at K15 startup (Start-Listener.bat), before the listener.

    If a session lock survived a restart, the watch loop died with us. Either
    the session is still live - resume watching it so its end still restores
    the TV - or it's dead, in which case just clear the lock so the chord
    isn't deaf. The TV is NOT touched on the dead path: its current state is
    unknowable after arbitrary downtime, and only a live session's end may
    drive it."""
    cglib.rotate_log()
    if cglib.lock_age() is None:
        return 0
    # A reconcile is its own intent (nobody asked for it; a restart caused
    # it), so it gets its own id rather than inheriting a dead session's.
    events.context(turn=events.new_turn())
    log("reconcile_found")
    for _ in range(3):                  # boot-time network may need a moment
        try:
            st = ssh("status")
            if st != "NOTREADY":
                log("reconcile_resumed")
                # Adopt, don't just touch: the owner note still names the dead
                # process, and release_lock at session end checks the pid.
                cglib.adopt_lock(f"{events.current().get('turn')} {os.getpid()}")
                # Whatever the marker says now IS this session's identity.
                watch(expected=st if events.valid_turn(st) else None)
                return 0
            break                       # definitive NOTREADY - session is dead
        except Exception:
            time.sleep(2)
    log.warn("reconcile_cleared", reason="dead_session")
    # Force-clear, not release_lock: the reconciler KNOWS the owner is dead
    # (that is the whole finding), and its pid would never match ours.
    cglib.LOCK.unlink(missing_ok=True)
    return 0


def usage():
    print("usage: couch.py [start [appid] [--turn <hex>]|reconcile]")
    return 2


def take_turn(argv):
    """Pull `--turn <id>` out of argv (mutating it) and return the id, or
    None. Hand-rolled rather than argparse: this lane's import list stays
    short by rule."""
    if "--turn" in argv:
        i = argv.index("--turn")
        turn = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
        return turn
    return None


if __name__ == "__main__":
    argv = sys.argv[1:]
    turn = take_turn(argv)
    cmd = argv[0] if argv else "start"
    if cmd == "start":
        if len(argv) > 1 and not argv[1].isdigit():
            sys.exit(usage())               # a non-digit appid is a caller bug
        appid = argv[1] if len(argv) > 1 else None
        sys.exit(start(appid, turn))
    elif cmd == "reconcile":
        sys.exit(reconcile())
    else:
        sys.exit(usage())
