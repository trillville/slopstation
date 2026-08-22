"""K15 orchestrator: Ex-Link TV power -> WoL -> ssh enter -> poll READY ->
switch TV input -> watch loop. The one rule: nothing switches the TV to the
gaming input before the host writes READY, so every pre-READY failure leaves
the TV exactly as the viewer had it."""
import os, socket, subprocess, sys, time

import cglib
import events

# Read on first use by cfg(), NOT at import. Every voice module reaches this
# file (dispatch imports couch for the ssh seam), so an import-time read made
# config.json a precondition for importing anything: `voice_agent.py
# --devices` on a box without one died with a raw FileNotFoundError before
# main() could say so, library.py and grammar_gate.py hid their couch/dispatch
# imports inside functions to dodge it, and doctor.py wrapped `from couch
# import ssh` in a try/except whose hint was "config broken?". Tests still
# assign couch.CFG directly and cfg() honours that.
CFG = None

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

log = cglib.make_log("launch")


def cfg():
    """config.json, loaded once on first use (see CFG above)."""
    global CFG
    if CFG is None:
        CFG = cglib.load_config()
    return CFG


def exlink(name, **fields):
    try:
        ack = cglib.exlink_send(name, cfg()["tvComPort"])
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
    mac = bytes.fromhex(cfg()["gamingPcMac"].replace(":", "").replace("-", ""))
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
    r = subprocess.run(["ssh", cfg()["sshHost"], cmd],
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
        try:
            with socket.create_connection((cfg()["gamingPcIp"], 22), 3):
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
    t0 = time.time()

    def ms():
        """Every milestone carries its distance from the chord/voice trigger,
        so time-to-READY is a distribution in Grafana rather than a number
        someone once measured by hand."""
        return round((time.time() - t0) * 1000)

    try:
        log("launch_start", appid=appid)
        exlink("power_on")
        wol()
        if not wait_port(): raise RuntimeError("gaming PC never became reachable")
        log("ssh_up", dur_ms=ms())

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
        exlink(cfg()["tvGamingCmd"])
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
        log.warn("launch_aborted", err=type(e).__name__, dur_ms=ms())
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
    exlink("power_off" if cfg()["tvOffWhenDone"] else cfg()["tvIdleCmd"])
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
