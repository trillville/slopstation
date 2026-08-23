"""K15 orchestrator: Ex-Link TV power -> WoL -> ssh enter -> poll READY ->
switch TV input -> watch loop. Invariant: nothing switches the TV to the
gaming input before the host writes READY, so a pre-READY failure leaves the
TV as the viewer had it."""
from __future__ import annotations

import os, socket, sys, time
from typing import Callable

import cglib
import events
import gamepc
import tv

PORT_WAIT_S    = 90    # PC power-on/resume until sshd answers
ENTER_ATTEMPTS = 60    # ~1/s; also covers waiting out logon after a cold boot
READY_WAIT_S   = 120   # Enter dispatch until the READY marker appears
WAKE_RETRY_S   = 30    # blind power_on re-send this far into the READY wait
                       # (the gaming PC has no Ex-Link). Past the healthy
                       # envelope: launches reach READY in ~9-20 s.
ENTER_REDISPATCH = 1   # extra Enter dispatches after a DETECTED death, not a
                       # timeout. All three recorded failures (2026-08-13
                       # 17:20, 08-16 17:56, 08-19 01:18) were TVs that acked
                       # power_on and stayed dark, burning the full window on
                       # an already-exited Enter. The retry gets a fresh one.
                       # Budget: 08-19's Enter died at 48.3 s (turn 7402df) and
                       # the K15 waited another 71 s on a dead task, so
                       # detection lands ~50 s in and a twice-failing launch
                       # takes ~170 s instead of 121 s. Safe to redispatch
                       # because Enter's abort path leaves OFFICE topology
                       # behind - the clean state a fresh Enter wants.
ENTER_SETTLE_S = 25    # how long a NOTREADY stays unremarkable: outlasts the
                       # gap between schtasks /Run returning (task only
                       # TRIGGERED) and it reading as running, plus the slowest
                       # launch that ever worked (19.8 s).
WATCH_POLL_S   = 5
WATCH_FAILS    = 3     # consecutive ssh failures (ssh() raises) = session
                       # dead. Low on purpose: a true sleep restores the TV in
                       # ~20-30 s, and a false positive only costs a desk-side
                       # relaunch (the Puck stays claimed, chord deaf).
TV_WAIT_S      = 30    # how long the enter_died rescue waits for the set to
                       # REPORT "on" before spending its redispatch or failing
                       # with the TV named. State flips ~5 s after an accepted
                       # frame (2026-08-19: standby t+1..4 s, on t+5 s).
TV_POKE_S      = 6     # power_on re-send interval while the set answers
                       # not-on; just past the ~5 s flip lag. power_on is
                       # discrete, safe to repeat.
TV_UNKNOWN_N   = 3     # unreadable answers before standing down to the blind
                       # path. None is UNKNOWN, never "off" (Wi-Fi blip, IP
                       # drift, no tvIp). Reads ride existing loops, ~0.5 s each.

log = cglib.make_log("launch")


class Cancelled(BaseException):
    """A requested stop of an in-flight launch. ssh `exit` alone stops only an
    Enter already RUNNING, so it can race this process's redispatch rescue and
    drop OFFICE topology plus a released Puck onto a live session; end_session
    also writes cglib.CANCEL, consumed by every wait in start(). BaseException
    on purpose, like KeyboardInterrupt: it must ride the abort handler
    (launch_aborted, lock released, no last_error), not launch_failed."""

    def __init__(self, by: str) -> None:
        self.by = by                    # the CANCELLING intent's turn, or ""
        super().__init__(by)


def raise_if_cancelled() -> None:
    """Consume a pending cancel (the unlink is the ack - a marker left behind
    would kill the next launch too) and stop through the abort path."""
    try:
        by = cglib.CANCEL.read_text().strip()
    except OSError:
        return                          # no marker - the overwhelming case
    try:
        cglib.CANCEL.unlink(missing_ok=True)
    except OSError:
        # Sharing violation (the writer's handle still open - CPython opens
        # without FILE_SHARE_DELETE) must not turn a stop into launch_failed.
        pass
    raise Cancelled(by)


def exlink(name: str, **fields) -> None:
    try:
        ack = tv.exlink_send(name, cglib.config()["tvComPort"])
        # ack = the receiver accepted the frame, not that the set acted on
        # it; Ex-Link is send-only and power_on can ack on a dark set.
        log("exlink_send", cmd=name, ack=ack or "no-ack", **fields)
    except Exception as e:
        # non-fatal: PC readiness is independent of whether the TV heard us
        log.error("exlink_nak", cmd=name, err=str(e), **fields)


def wol() -> None:
    mac = bytes.fromhex(cglib.config()["gamingPcMac"].replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
    log("wol_sent")


def wait_port(timeout: float = PORT_WAIT_S) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        # A cold boot can spend the whole 90 s here; a cancel must not wait it out.
        raise_if_cancelled()
        try:
            with socket.create_connection((cglib.config()["gamingPcIp"], 22), 3):
                return True
        except OSError:
            time.sleep(1)
    return False


class TvEvidence:
    """The set's own word, read during start()'s READY wait on tvIp rigs.
    Never gates Enter - the panel's ~5 s flip lag would tax every healthy
    launch (36 of 38 land in a 9.1-12.9 s band). It re-pokes power_on early
    (TV_POKE_S), so a set taking the second frame lights inside Enter's
    retry-apply window, and it gates the enter_died rescue. Every recorded
    refusal acked power_on and stayed dark - the serial receiver is powered
    in standby - and the PC cannot see the panel either (EDID and all three
    WMI monitor classes read identically awake or asleep, 2026-08-13).
    Unreadable (no tvIp, IP drift, Wi-Fi blip) is not refused: fail open,
    and fail closed only on the set's own word at the rescue. Idle duration
    does not predict a refusal: failures at 11.8-20.0 h since the last
    session, successes at 22+ h."""

    def __init__(self, ip: str | None, first: str | None, ms: Callable[[], int]) -> None:
        self.ip = ip
        self.confirmed = False      # the set answered "on" at least once
        self.gave_up = False        # stood down: TV_UNKNOWN_N unreadable answers
        self.last = first           # last raw answer, for the error text
        self._unknowns = 0
        self._poke_at = time.time() + TV_POKE_S
        self._ms = ms               # elapsed-since-intent, for the milestones

    def undecided(self) -> bool:
        """A tvIp rig whose set has neither answered "on" nor been given up on."""
        return bool(self.ip) and not self.confirmed and not self.gave_up

    def poll(self) -> None:
        """One evidence step, called from waits that already loop: latch
        "on", stand down on persistent silence, re-poke while not-on.

        Tune TV_POKE_S / TV_WAIT_S against warm-PC launches only (ssh_up
        ~0.2 s), where dur_ms approximates frame-to-lit."""
        if not self.undecided():
            return
        self.last = tv.tv_power_state(self.ip, timeout=0.5, raw=True)
        if self.last == "on":
            self.confirmed = True
            # dur_ms is elapsed-since-intent and polling starts only after
            # wait_port, so a COLD boot censors it (~20-90 s of boot with
            # the panel long since lit); warm-PC launches approximate
            # frame-to-lit.
            log("tv_on", dur_ms=self._ms())
            return
        if self.last is None:
            self._unknowns += 1
            if self._unknowns >= TV_UNKNOWN_N:
                self.gave_up = True
                log.warn("tv_state_unknown", dur_ms=self._ms())
            return
        self._unknowns = 0
        if time.time() >= self._poke_at:
            exlink("power_on", again=True)
            self._poke_at = time.time() + TV_POKE_S


def wait_ready(turn: str, evidence: TvEvidence,
               dispatch_enter: Callable[..., bool], ms: Callable[[], int]) -> None:
    """Poll the host until the READY marker echoes `turn`, re-dispatching a
    DETECTED dead Enter once (ENTER_REDISPATCH); raises once the window
    closes. `dispatch_enter` is start()'s closure, so its enter_sent reaches
    the abort handler; `evidence` gates the rescue."""
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
        evidence.poll()
        # Blind wake retry for a set that slept through the launch_start
        # power_on. WAKE_RETRY_S lands the frame after Enter's first
        # profile check has failed and before its retry apply, the only
        # window where waking now still rescues this launch (Enter runs to
        # ~66 s: 20 s check, OFFICE restore, retry up to 20 s).
        if repoke_at and time.time() >= repoke_at:
            exlink("power_on", again=True)
            repoke_at = None
        try:
            st = gamepc.status()
            # The marker echoes the turn Enter was given: ours = ready.
            if st == turn:
                log("host_ready", status=st, dur_ms=ms(), verified=True)
                ready = True; break
            if st != "NOTREADY":
                if events.valid_turn(st):
                    # Someone else's turn: a stale marker. Keep waiting,
                    # our Enter overwrites it; treating any non-NOTREADY as
                    # ready switched the TV to a host still mid-Enter.
                    if st != foreign_seen:
                        log.warn("ready_foreign", status=st)
                        foreign_seen = st
                else:
                    # ISO timestamp: a PC deployed before turn-stamping.
                    # Accept, but record unverified.
                    log("host_ready", status=st, dur_ms=ms(),
                        verified=False)
                    ready = True; break
        except Exception as e:
            log.warn("status_poll_failed", err=str(e))
            time.sleep(1); continue
        # Still NOTREADY. An Enter no longer running will never write the
        # marker, so the rest of the window is dead time: re-dispatch.
        if (st == "NOTREADY" and time.time() >= settle_at
                and gamepc.enter_running() is False):
            # Enter writes the marker and THEN exits, so one (NOTREADY,
            # task-idle) pair can be those instants read in the wrong
            # order. Demand it twice: a marker that landed in between is
            # taken by the next poll, and no redispatch hits a live session.
            idle_seen += 1
            if idle_seen >= 2:
                log.warn("enter_died", dur_ms=ms())
                # A cancel that landed while the death was being proven
                # must win before the second Enter; the loop-top check is
                # one iteration too coarse.
                raise_if_cancelled()
                if not redispatches:
                    raise RuntimeError("Enter exited without READY")
                # Spend the one redispatch on a lit panel; never "on"
                # inside TV_WAIT_S is a refusal. Confirmed-on and
                # unreadable rigs skip this wait.
                rescue_by = time.time() + TV_WAIT_S
                while evidence.undecided():
                    if time.time() >= rescue_by:
                        raise RuntimeError(
                            f"TV never reported on (PowerState="
                            f"{evidence.last!r} after {TV_WAIT_S}s of asking) "
                            "- the set is refusing the wake, not "
                            "missing the frame")
                    cglib.touch_lock()
                    raise_if_cancelled()
                    evidence.poll()
                    time.sleep(1)
                exlink("power_on", again=True)
                if not dispatch_enter("enter_redispatched", attempts=5):
                    raise RuntimeError("Enter died and could not be re-triggered")
                redispatches -= 1
                idle_seen = 0
                settle_at = time.time() + ENTER_SETTLE_S
                # A full window of its own; leftovers cannot reach READY.
                end = time.time() + READY_WAIT_S
        else:
            idle_seen = 0
        time.sleep(1)
    if not ready: raise RuntimeError("host never reported READY")


def start(appid: str | None = None, turn: str | None = None) -> int:
    # One id per intent, minted upstream; a direct run mints its own.
    turn = turn if events.valid_turn(turn) else events.new_turn()
    events.context(turn=turn)
    # Before the lock: a config doctor would FAIL must not reach power_on, and
    # must not die holding the lock.
    try:
        missing = cglib.missing_config(cglib.config())
    except Exception as e:
        missing, err = [], str(e)
    else:
        err = None
    if missing or err:
        log.error("config_invalid", missing=missing or None, err=err)
        try:
            cglib.LAST_ERROR.write_text(f"config.json: {err or f'missing {missing}'}")
        except OSError:
            pass
        return 2
    # The pre-read only shapes the log lines - acquire_lock is the arbiter.
    age = cglib.lock_age()
    if cglib.session_active(age):
        log("launch_busy", lock_age_s=round(age)); return 1
    if age is not None:
        log.warn("lock_recycled", lock_age_s=round(age))
    if not cglib.acquire_lock(f"{turn} {os.getpid()}"):
        log("launch_busy", reason="lost_acquire_race"); return 1
    # The Cancelled handler keys on this: a cancel consumed after our enter
    # went out may have raced the exit that wrote it.
    enter_sent = False
    # A cancel predating this intent is void. Guarded because this runs
    # outside the try below and an unhandled OSError would die with the lock
    # held.
    try:
        cglib.CANCEL.unlink(missing_ok=True)
    except OSError as e:
        log.warn("cancel_void_failed", err=str(e))
    t0 = time.time()

    def ms():
        """Milliseconds since the chord/voice trigger, on every milestone."""
        return round((time.time() - t0) * 1000)

    try:
        tv_ip = cglib.config().get("tvIp")
        # Raw depth rung as the launch FOUND the set: "on", "standby", ""
        # (deep: hours off, IP server still answering with PowerState drained)
        # or the "unreachable" sentinel - events.emit drops None-valued fields,
        # so an unreachable set would otherwise log like a rig with no tvIp.
        # Ex-Link cannot answer this: its ack is a constant 030cf1 whatever
        # the power state or command, nothing past three bytes (2026-08-19).
        # Do not re-run that probe and do not brute-force the command space
        # hunting a status frame - the same protocol carries service-mode
        # commands, and a valid-checksum guess is not safe to fire at a TV
        # someone is watching.
        tv0 = tv.tv_power_state(tv_ip, timeout=0.5, raw=True) if tv_ip else None
        log("launch_start", appid=appid,
            **({"tv": tv0 if tv0 is not None else "unreachable"} if tv_ip else {}))
        exlink("power_on")
        wol()
        if not wait_port(): raise RuntimeError("gaming PC never became reachable")
        log("ssh_up", dur_ms=ms())

        evidence = TvEvidence(tv_ip, tv0, ms)

        def dispatch_enter(event, attempts=ENTER_ATTEMPTS):
            """Trigger the Enter task; True once Dispatch answered OK.
            `attempts` is per-call: the first dispatch also waits out logon
            after a cold boot, a re-dispatch must not spend the READY window."""
            nonlocal enter_sent
            refused = set()
            for _ in range(attempts):
                cglib.touch_lock()
                raise_if_cancelled()
                try:
                    answer = gamepc.enter()
                    if answer == "OK":
                        enter_sent = True
                        log(event, dur_ms=ms()); return True
                    if answer not in refused:   # NOTASK:Enter / FAILED:<code> / DENIED
                        log.warn("enter_refused", answer=answer)
                        refused.add(answer)
                except Exception as e:
                    log.warn("enter_retry", err=str(e))
                time.sleep(1)
            return False

        if not dispatch_enter("enter_dispatched"):
            raise RuntimeError("could not trigger Enter task")
        wait_ready(turn, evidence, dispatch_enter, ms)
        cglib.LAST_ERROR.unlink(missing_ok=True)   # success supersedes any old failure
        exlink(cglib.config()["tvGamingCmd"])
        if appid:
            # Voice "play <title>" from cold. Best-effort - Big Picture up is
            # a working outcome, except on ALREADY (appid already running from
            # an EARLIER session), where the couch cannot drive it: warn
            # (2026-08-13 turn 14852d).
            try:
                answer = gamepc.launch(appid)
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
        # Ctrl-C is a KeyboardInterrupt, not an Exception, so the handler above
        # never sees it; without this clause the lane dies silently, holding
        # the lock until staleness recycles it (2026-08-16 b43b74). Cancelled
        # lands here too, and neither writes LAST_ERROR.
        by = {"cancelled_by": e.by} if isinstance(e, Cancelled) and e.by else {}
        log.warn("launch_aborted", err=type(e).__name__, dur_ms=ms(), **by)
        if isinstance(e, Cancelled) and enter_sent:
            # The canceller's exit stops a RUNNING Enter, but one still inside
            # the schtasks trigger gap when Exit finishes runs to completion,
            # claiming the Puck and TV-GAMING with no watcher alive (~7 s of
            # exit variance, 0b785e). Our own exit, strictly after our last
            # enter, closes the ordering.
            try:
                gamepc.exit()
                log("exit_dispatched", reason="cancel_after_enter")
            except Exception:
                pass
        cglib.release_lock(); return 1
    return 0


def watch(expected: str | None = None) -> None:
    """Poll the session until it ends, then restore the TV and release the
    lock. `expected` is the turn the marker should keep echoing; a different
    one means a successor owns the rig, so leave the TV and lock to it. None
    disables the check."""
    fails = 0
    died_by_fails = False
    while True:
        time.sleep(WATCH_POLL_S)
        cglib.touch_lock()
        try:
            st = gamepc.status(); fails = 0
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
        # A fails-death can leave a live PC holding the Puck in TV topology,
        # and a held Puck means a deaf chord; if it is only a blip, the PC's
        # teardown releases it.
        try:
            if gamepc.exit() == "OK":
                log("exit_dispatched", reason="release_puck_after_ssh_fails")
        except Exception:
            pass
    exlink("power_off" if cglib.config()["tvOffWhenDone"] else cglib.config()["tvIdleCmd"])
    # Ownership-checked: a lock recycled while we stalled belongs to the
    # successor, and unlinking it would free a live session.
    if not cglib.release_lock():
        log.warn("lock_kept", reason="owned_by_successor")
    log("session_idle")


def reconcile() -> int:
    """Run once at K15 startup (Start-Listener.bat), before the listener.

    A surviving session lock means the watch loop died with us: resume
    watching a live session so its end still restores the TV, or clear the
    lock so the chord isn't deaf. The TV is NOT touched on the dead path."""
    cglib.rotate_log()
    if cglib.lock_age() is None:
        return 0
    # A reconcile is its own intent: new id, not the dead session's.
    events.context(turn=events.new_turn())
    log("reconcile_found")
    answered = False
    for _ in range(3):                  # boot-time network may need a moment
        try:
            st = gamepc.status()
            answered = True
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
    log.warn("reconcile_cleared",
             reason="dead_session" if answered else "unreachable")
    # Force-clear, not release_lock: the owner is known dead and its pid would
    # never match ours.
    cglib.LOCK.unlink(missing_ok=True)
    return 0


def usage() -> int:
    print("usage: couch.py [start [appid] [--turn <hex>]|reconcile]")
    return 2


def take_turn(argv: list[str]) -> str | None:
    """Pull `--turn <id>` out of argv (mutating it) and return it, or None."""
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
