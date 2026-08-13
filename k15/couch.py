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
WATCH_POLL_S   = 5
WATCH_FAILS    = 3     # consecutive ssh failures (raised, see ssh()) = session
                       # dead. Deliberately low: a true sleep restores the TV in
                       # ~20-30s; the false-positive cost of a transient outage
                       # is a desk-side relaunch (the Puck stays claimed, so the
                       # chord can't hear).

log = cglib.make_log("launch")


def exlink(name):
    try:
        ack = cglib.exlink_send(name, CFG["tvComPort"])
        log("exlink_send", cmd=name, ack=ack or "no-ack")
    except Exception as e:
        # non-fatal: PC readiness is independent of whether the TV heard us
        log.error("exlink_nak", cmd=name, err=str(e))


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
    (status/games/playing) deliberately go through plain ssh(): they are not
    intents, and tagging them would just multiply the id across noise.

    `turn` is EXPLICIT for callers that cannot rely on the ambient one, and
    that is not a hypothetical convenience - it shipped broken. A ContextVar
    reaches only tasks created after it is set. The voice agent mints its turn
    inside an already-running frame processor, so the task that later calls
    dispatch holds an older snapshot and sees nothing: on 2026-08-11 every
    voice-driven exit reached the gaming PC uncorrelated, and the launch ran
    under an id couch.py minted for itself rather than the one the user's
    sentence created. Ambient remains the default for couch.py's own
    in-process calls, where it does propagate."""
    turn = turn or events.current().get("turn")
    # Re-validate at the wire, not just at mint: Dispatch fails CLOSED on a
    # malformed id (it would match no verb and answer DENIED), so a telemetry
    # bug must not be able to take launches down with it. Uncorrelated beats
    # refused, every time.
    return ssh(f"{cmd} --turn {turn}" if events.valid_turn(turn) else cmd, **kw)


def wait_port(timeout=PORT_WAIT_S):
    end = time.time() + timeout
    while time.time() < end:
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
    # The pre-read only shapes the log lines; acquire_lock's exclusive create
    # is the arbiter. Losing it means another launch won inside the last few
    # milliseconds - the window that used to let a chord and a voice start
    # both dispatch (and the second Enter recycle the Puck under the first).
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
        for _ in range(ENTER_ATTEMPTS):
            cglib.touch_lock()
            try:
                if ssh_intent("enter") == "OK":
                    log("enter_dispatched", dur_ms=ms()); break
            except Exception as e:
                log.warn("enter_retry", err=str(e))
            time.sleep(1)
        else: raise RuntimeError("could not trigger Enter task")
        end = time.time() + READY_WAIT_S
        ready = False
        foreign_seen = None
        while time.time() < end:
            cglib.touch_lock()
            try:
                st = ssh("status")
                if st == turn:
                    # Generation identity: this READY echoes OUR turn, so it
                    # answers THIS launch. Enter writes the id that rode in
                    # with the enter verb, so verified is the normal case.
                    log("host_ready", status=st, dur_ms=ms(), verified=True)
                    ready = True; break
                if st != "NOTREADY":
                    if events.valid_turn(st):
                        # Turn-shaped but not ours: a stale marker from a
                        # session nothing cleaned up. Keep waiting - our
                        # Enter overwrites it on completion, so this
                        # converges instead of switching the TV to a host
                        # still mid-Enter (which is what "any non-NOTREADY
                        # is ready" used to do here). One rare corner also
                        # lands here failing CLOSED: another mutating verb
                        # overwriting the turn file inside the ~1 s before
                        # Enter reads it makes a REAL ready look foreign -
                        # the launch times out clean and a retry works,
                        # which beats the old fail-open every time.
                        if st != foreign_seen:
                            log.warn("ready_foreign", status=st)
                            foreign_seen = st
                    else:
                        # Legacy shape (ISO timestamp): a PC deployed before
                        # turn-stamping, or an Enter with no turn to echo.
                        # Accept - either side may deploy first - but say so.
                        log("host_ready", status=st, dur_ms=ms(),
                            verified=False)
                        ready = True; break
            except Exception as e:
                log.warn("status_poll_failed", err=str(e))
            time.sleep(1)
        if not ready: raise RuntimeError("host never reported READY")
        cglib.LAST_ERROR.unlink(missing_ok=True)   # success supersedes any old failure
        exlink(CFG["tvGamingCmd"])
        if appid:
            # Voice "play <title>" from cold: queue the game once the session
            # is READY. Best-effort - a failed game launch never fails the
            # session; Big Picture being up is already a working outcome.
            try:
                log("game_launch", appid=appid,
                    result=ssh_intent(f"launch {appid}"))
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
    return 0


def watch(expected=None):
    """Poll the session until it ends, then restore the TV and release the
    lock. `expected` is the turn the READY marker should keep echoing; a
    marker that changes to a DIFFERENT turn means a successor session owns
    the rig (this watcher must have stalled past staleness for its launch to
    acquire), so leave everything - TV and lock alike - to the owner. None
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
    # Ownership-checked: if this watcher stalled past staleness and a new
    # launch recycled the lock meanwhile, the file is the successor's now and
    # stays. (The stall itself is the anomaly; refusing the unlink just keeps
    # a live session's arbiter intact.)
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
                # The marker's CURRENT value is the session's identity from
                # here on - if it changes to another turn, a successor owns
                # the rig and this watcher stands down.
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
    """Pull `--turn <id>` out of argv (mutating it) and return the id, or None.
    Hand-rolled rather than argparse to keep this lane's import list as short
    as it has always been."""
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
