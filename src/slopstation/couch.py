"""Start, monitor, and stop couch-gaming sessions."""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Callable

from slopstation import config, events, gamepc, logbook, sessionlock, statefile, tv

PORT_WAIT_S = 90  # PC power-on/resume until sshd answers
ENTER_ATTEMPTS = 60  # ~1/s; also covers waiting out logon after a cold boot
READY_WAIT_S = 120  # Enter dispatch until the READY marker appears
WAKE_RETRY_S = 30  # retry TV power while waiting for the gaming PC
ENTER_REDISPATCH = 1  # retries after the Enter task exits before READY
ENTER_SETTLE_S = 25
WATCH_POLL_S = 5
WATCH_FAILS = 3  # consecutive SSH failures that end a session
TV_WAIT_S = 30
TV_POKE_S = 6  # power_on re-send interval while the set answers not-on
TV_UNKNOWN_N = 3  # unreadable TV states allowed before stopping the checks

log = logbook.logger("launch")


class Cancelled(BaseException):
    """Raised when an in-progress launch is cancelled."""

    def __init__(self, by: str) -> None:
        self.by = by  # the CANCELLING intent's turn, or ""
        super().__init__(by)


class Superseded(BaseException):
    """The session belongs to another process; stop without sending Exit."""


def raise_if_cancelled() -> None:
    """Consume a cancellation marker and stop the current launch."""
    if not sessionlock.touch():
        raise Superseded
    try:
        by = sessionlock.cancel_file().read_text().strip()
    except OSError:
        return  # no marker - the overwhelming case
    try:
        sessionlock.cancel_file().unlink(missing_ok=True)
    except OSError:
        # The writer may still have the marker open on Windows.
        pass
    raise Cancelled(by)


def tv_command(name: str, **fields) -> None:
    try:
        device = tv.Tv(config.current(), log)
        if name == "power_on":
            device.power_on(**fields)
        elif name == "power_off":
            device.power_off(**fields)
        else:
            device.select_input(name, **fields)
    except Exception:
        pass  # Device logs the failure; launch readiness handles the outcome.


def restore_tv() -> None:
    """Put the TV back the way a finished session leaves it."""
    cfg = config.current()
    tv_command("power_off" if cfg["tvOffWhenDone"] else cfg["tvIdleCmd"])


def finish_session(restore: bool, exit_reason: str | None = None) -> bool:
    """Teardown only while we own the session, then let the next launch acquire it."""

    def teardown():
        if exit_reason:
            try:
                if gamepc.exit() == "OK":
                    log("exit_dispatched", reason=exit_reason)
            except Exception:
                pass
        if restore:
            restore_tv()

    return sessionlock.release(teardown)


def wol() -> None:
    mac = bytes.fromhex(
        config.current()["gamingPcMac"].replace(":", "").replace("-", "")
    )
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
    log("wol_sent")


def wait_port(timeout: float = PORT_WAIT_S) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        raise_if_cancelled()
        try:
            with socket.create_connection((config.current()["gamingPcIp"], 22), 3):
                return True
        except OSError:
            time.sleep(1)
    return False


class TvEvidence:
    """Track the TV's reported power state while the gaming PC starts."""

    def __init__(
        self, ip: str | None, first: str | None, ms: Callable[[], int]
    ) -> None:
        self.ip = ip
        self.confirmed = False  # the set answered "on" at least once
        self.gave_up = False  # stood down: TV_UNKNOWN_N unreadable answers
        self.last = first  # last raw answer, for the error text
        self._unknowns = 0
        self._poke_at = time.time() + TV_POKE_S
        self._ms = ms  # elapsed-since-intent, for the milestones

    def undecided(self) -> bool:
        """Return whether TV power checks should continue."""
        return bool(self.ip) and not self.confirmed and not self.gave_up

    def poll(self) -> None:
        """Poll the TV, retry power when off, and stop after repeated errors."""
        if self.ip is None or not self.undecided():
            return
        self.last = tv.Tv({"tvIp": self.ip}, log).power_state(timeout=0.5, raw=True)
        if self.last == "on":
            self.confirmed = True
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
            tv_command("power_on", again=True)
            self._poke_at = time.time() + TV_POKE_S


def wait_ready(
    turn: str,
    evidence: TvEvidence,
    dispatch_enter: Callable[..., bool],
    ms: Callable[[], int],
) -> None:
    """Wait for the matching READY marker and retry a stopped Enter task."""
    end = time.time() + READY_WAIT_S
    foreign_seen = None
    redispatches = ENTER_REDISPATCH
    idle_seen = 0
    settle_at = time.time() + ENTER_SETTLE_S
    repoke_at: float | None = time.time() + WAKE_RETRY_S
    while time.time() < end:
        raise_if_cancelled()
        evidence.poll()
        # Retry once in case the TV missed the initial power command.
        if repoke_at and time.time() >= repoke_at:
            tv_command("power_on", again=True)
            repoke_at = None
        try:
            st = gamepc.status()
            if st == turn:
                log("host_ready", status=st, dur_ms=ms(), verified=True)
                return
            if st != "NOTREADY":
                if events.valid_turn(st):
                    # A marker for another launch does not make this one ready.
                    if st != foreign_seen:
                        log.warn("ready_foreign", status=st)
                        foreign_seen = st
                else:
                    # Accept legacy timestamp markers, but mark them unverified.
                    log("host_ready", status=st, dur_ms=ms(), verified=False)
                    return
        except Exception as e:
            log.warn("status_poll_failed", err=str(e))
            time.sleep(1)
            continue
        if (
            st == "NOTREADY"
            and time.time() >= settle_at
            and gamepc.enter_running() is False
        ):
            # Require two idle checks to avoid racing the READY marker write.
            idle_seen += 1
            if idle_seen >= 2:
                log.warn("enter_died", dur_ms=ms())
                raise_if_cancelled()
                if not redispatches:
                    raise RuntimeError("Enter exited without READY")
                # Do not restart Enter while a reachable TV still reports off.
                rescue_by = time.time() + TV_WAIT_S
                while evidence.undecided():
                    if time.time() >= rescue_by:
                        raise RuntimeError(
                            f"TV never reported on (PowerState="
                            f"{evidence.last!r} after {TV_WAIT_S}s of asking) "
                            "- the set is refusing the wake, not "
                            "missing the frame"
                        )
                    raise_if_cancelled()
                    evidence.poll()
                    time.sleep(1)
                tv_command("power_on", again=True)
                if not dispatch_enter("enter_redispatched", attempts=5):
                    raise RuntimeError("Enter died and could not be re-triggered")
                redispatches -= 1
                idle_seen = 0
                settle_at = time.time() + ENTER_SETTLE_S
                end = time.time() + READY_WAIT_S
        else:
            idle_seen = 0
        time.sleep(1)
    raise RuntimeError("host never reported READY")


def start(appid: str | None = None, turn: str | None = None) -> int:
    turn = turn if events.valid_turn(turn) else events.new_turn()
    events.context(turn=turn)
    err: str | None = None
    try:
        missing = config.missing(config.current())
    except Exception as e:
        missing, err = [], str(e)
    if missing or err:
        log.error("config_invalid", missing=missing or None, err=err)
        try:
            sessionlock.last_error_file().write_text(
                f"config.json: {err or f'missing {missing}'}"
            )
        except OSError:
            pass
        return 2
    age = sessionlock.age()
    if sessionlock.active(age):
        log("launch_busy", lock_age_s=round(age or 0))
        return 1
    if age is not None:
        log.warn("lock_recycled", lock_age_s=round(age))
    if not sessionlock.acquire(f"{turn} {os.getpid()}"):
        log("launch_busy", reason="lost_acquire_race")
        return 1
    enter_sent = False
    tv_woken = False
    # A cancellation left by an earlier launch does not apply here.
    try:
        sessionlock.cancel_file().unlink(missing_ok=True)
    except OSError as e:
        log.warn("cancel_void_failed", err=str(e))
    t0 = time.time()

    def ms():
        """Return elapsed milliseconds for event timing."""
        return round((time.time() - t0) * 1000)

    try:
        tv_ip = config.current().get("tvIp")
        # Use an explicit value to distinguish an unreachable TV from no tvIp.
        tv0 = tv.Tv(config.current(), log).power_state(timeout=0.5, raw=True)
        log(
            "launch_start",
            appid=appid,
            **({"tv": tv0 if tv0 is not None else "unreachable"} if tv_ip else {}),
        )
        tv_command("power_on")
        # With power, not after READY: the set wakes on HDMI 4 (measured 2026-09-03).
        tv_command(config.current()["tvGamingCmd"])
        # Only restore power on failure if this launch woke the TV.
        tv_woken = tv0 != "on"
        wol()
        if not wait_port():
            raise RuntimeError("gaming PC never became reachable")
        log("ssh_up", dur_ms=ms())

        evidence = TvEvidence(tv_ip, tv0, ms)

        def dispatch_enter(event, attempts=ENTER_ATTEMPTS):
            """Trigger the Enter task, retrying until it starts."""
            nonlocal enter_sent
            refused = set()
            for _ in range(attempts):
                raise_if_cancelled()
                try:
                    answer = gamepc.enter()
                    if answer == "OK":
                        enter_sent = True
                        log(event, dur_ms=ms())
                        return True
                    if answer not in refused:  # NOTASK:Enter / FAILED:<code> / DENIED
                        log.warn("enter_refused", answer=answer)
                        refused.add(answer)
                except Exception as e:
                    log.warn("enter_retry", err=str(e))
                time.sleep(1)
            return False

        if not dispatch_enter("enter_dispatched"):
            raise RuntimeError("could not trigger Enter task")
        wait_ready(turn, evidence, dispatch_enter, ms)
        sessionlock.last_error_file().unlink(missing_ok=True)  # success supersedes it
        tv_command(config.current()["tvGamingCmd"])
        if appid:
            try:
                answer = gamepc.launch(appid)
                emit = log.warn if answer == "ALREADY" else log
                emit("game_launch", appid=appid, result=answer)
            except Exception as e:
                log.warn("game_launch_failed", appid=appid, err=str(e))
        log("session_gaming", dur_ms=ms())
        watch(expected=turn)
    except Exception as e:
        log.error("launch_failed", err=str(e), dur_ms=ms())
        try:
            sessionlock.last_error_file().write_text(str(e))
        except OSError:
            pass
        finish_session(tv_woken)
        return 1
    except BaseException as e:
        # Cancellation and Ctrl-C still need to release the session lock.
        by = {"cancelled_by": e.by} if isinstance(e, Cancelled) and e.by else {}
        log.warn("launch_aborted", err=type(e).__name__, dur_ms=ms(), **by)
        finish_session(
            tv_woken,
            "cancel_after_enter" if isinstance(e, Cancelled) and enter_sent else None,
        )
        return 1
    return 0


def watch(expected: str | None = None) -> None:
    """Monitor a session, then restore the TV and release its lock."""
    fails = 0
    died_by_fails = False
    while True:
        time.sleep(WATCH_POLL_S)
        if not sessionlock.touch():
            log.warn("session_ended", reason="superseded")
            return
        try:
            st = gamepc.status()
            fails = 0
            if st == "NOTREADY":
                log("session_ended", reason="host")
                break
            if expected and events.valid_turn(st) and st != expected:
                log.warn("session_ended", reason="superseded", status=st)
                return
        except Exception:
            fails += 1
            if fails >= WATCH_FAILS:
                log.warn("session_ended", reason="ssh_fails", fails=fails)
                died_by_fails = True
                break

    if not finish_session(
        True, "release_puck_after_ssh_fails" if died_by_fails else None
    ):
        log.warn("lock_kept", reason="owned_by_successor")
    log("session_idle")


def reconcile() -> int:
    """Resume monitoring a live session or clear an abandoned lock at startup."""
    logbook.rotate()
    try:
        previous = sessionlock.lock_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    events.context(turn=events.new_turn())
    log("reconcile_found")
    answered = False
    for _ in range(3):  # boot-time network may need a moment
        try:
            st = gamepc.status()
            answered = True
            if st != "NOTREADY":
                log("reconcile_resumed")
                if sessionlock.adopt(
                    f"{events.current().get('turn')} {os.getpid()}", previous
                ):
                    watch(expected=st if events.valid_turn(st) else None)
                return 0
            break  # definitive NOTREADY - session is dead
        except Exception:
            time.sleep(2)
    log.warn("reconcile_cleared", reason="dead_session" if answered else "unreachable")
    # The abandoned lock belongs to another process, so release() cannot own it.
    # Acquisition and stale-lock cleanup must not race each other.
    with statefile.guard(sessionlock.lock_file()):
        try:
            if sessionlock.lock_file().read_text(encoding="utf-8") == previous:
                sessionlock.lock_file().unlink()
        except FileNotFoundError:
            pass
    return 0


def usage() -> int:
    print("usage: python -m slopstation.couch [start [appid] [--turn <hex>]|reconcile]")
    return 2


def take_turn(argv: list[str]) -> str | None:
    """Pull `--turn <id>` out of argv (mutating it) and return it, or None."""
    if "--turn" in argv:
        i = argv.index("--turn")
        turn = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i : i + 2]
        return turn
    return None


if __name__ == "__main__":
    argv = sys.argv[1:]
    turn = take_turn(argv)
    cmd = argv[0] if argv else "start"
    if cmd == "start":
        if len(argv) > 1 and not argv[1].isdigit():
            sys.exit(usage())  # a non-digit appid is a caller bug
        appid = argv[1] if len(argv) > 1 else None
        sys.exit(start(appid, turn))
    elif cmd == "reconcile":
        sys.exit(reconcile())
    else:
        sys.exit(usage())
