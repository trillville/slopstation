"""K15 orchestrator: Ex-Link TV power -> WoL -> ssh enter -> poll READY ->
switch TV input -> watch loop. The one rule: nothing switches the TV to the
gaming input before the host writes READY, so every pre-READY failure leaves
the TV exactly as the viewer had it."""
import socket, subprocess, sys, time

import cglib
from cglib import LOCK, LOCK_STALE_S

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


def touch_lock():
    try: LOCK.write_text(str(time.time()))
    except OSError: pass


def exlink(name):
    try:
        ack = cglib.exlink_send(name, CFG["tvComPort"])
        log(f"exlink {name} -> {ack or 'no-ack'}")
    except Exception as e:
        log(f"exlink {name} FAILED: {e}")   # non-fatal: PC readiness is independent


def wol():
    mac = bytes.fromhex(CFG["gamingPcMac"].replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
    log("WOL sent")


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


def wait_port(timeout=PORT_WAIT_S):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((CFG["gamingPcIp"], 22), 3):
                return True
        except OSError:
            time.sleep(1)
    return False


def start(appid=None):
    age = cglib.lock_age()
    if age is not None and age < LOCK_STALE_S:
        log("session already active/starting - ignoring"); return 1
    if age is not None:
        log(f"stale session lock ({age:.0f}s old, owner dead) - recycling")
    LOCK.parent.mkdir(exist_ok=True); touch_lock()
    try:
        log("=== LAUNCH ===")
        exlink("power_on")
        wol()
        if not wait_port(): raise RuntimeError("gaming PC never became reachable")
        log("ssh port up")
        for _ in range(ENTER_ATTEMPTS):
            touch_lock()
            try:
                if ssh("enter") == "OK":
                    log("enter dispatched"); break
            except Exception as e:
                log(f"enter attempt failed ({e}) - retrying")
            time.sleep(1)
        else: raise RuntimeError("could not trigger Enter task")
        end = time.time() + READY_WAIT_S
        ready = False
        while time.time() < end:
            touch_lock()
            try:
                st = ssh("status")
                if st != "NOTREADY":
                    log(f"host READY ({st})"); ready = True; break
            except Exception as e:
                log(f"status poll failed ({e}) - retrying")
            time.sleep(1)
        if not ready: raise RuntimeError("host never reported READY")
        cglib.LAST_ERROR.unlink(missing_ok=True)   # success supersedes any old failure
        exlink(CFG["tvGamingCmd"])
        if appid:
            # Voice "play <title>" from cold: queue the game once the session
            # is READY. Best-effort - a failed game launch never fails the
            # session; Big Picture being up is already a working outcome.
            try:
                log(f"game launch {appid} -> {ssh(f'launch {appid}')}")
            except Exception as e:
                log(f"game launch {appid} failed ({e}) - session continues")
        log("=== GAMING ==="); watch()
    except Exception as e:
        log(f"launch failed: {e} - TV input untouched")
        try:
            # The listener polls this and signals the failure haptically (3 thuds).
            cglib.LAST_ERROR.write_text(str(e))
        except OSError:
            pass
        LOCK.unlink(missing_ok=True); return 1
    return 0


def watch():
    fails = 0
    died_by_fails = False
    while True:
        time.sleep(WATCH_POLL_S)
        touch_lock()
        try:
            st = ssh("status"); fails = 0
            if st == "NOTREADY":
                log("host reports session ended"); break
        except Exception:
            fails += 1
            if fails >= WATCH_FAILS:
                log("gaming PC gone (slept/crashed) - treating as ended")
                died_by_fails = True
                break
    if died_by_fails:
        # A fails-death can leave a live PC holding the Puck in TV topology -
        # and a held Puck means a deaf chord, so recovery would need the desk.
        # Best-effort exit dispatch: if the PC is actually alive behind a
        # transient blip, its teardown restores the desk and sends the Puck
        # home; if it's truly asleep, this raises and nothing changes.
        try:
            if ssh("exit") == "OK":
                log("watch died on ssh failures - dispatched exit to release the desk/Puck")
        except Exception:
            pass
    exlink("power_off" if CFG["tvOffWhenDone"] else CFG["tvIdleCmd"])
    LOCK.unlink(missing_ok=True)
    log("=== IDLE ===")


def reconcile():
    """Run once at K15 startup (Start-Listener.bat), before the listener.

    If a session lock survived a restart, the watch loop died with us. Either
    the session is still live - resume watching it so its end still restores
    the TV - or it's dead, in which case just clear the lock so the chord
    isn't deaf. The TV is NOT touched on the dead path: its current state is
    unknowable after arbitrary downtime, and only a live session's end may
    drive it."""
    cglib.rotate_log()
    if not LOCK.exists():
        return 0
    log("reconcile: session lock survived a restart - checking the host")
    for _ in range(3):                  # boot-time network may need a moment
        try:
            if ssh("status") != "NOTREADY":
                log("reconcile: session still live - resuming watch")
                touch_lock()
                watch()
                return 0
            break                       # definitive NOTREADY - session is dead
        except Exception:
            time.sleep(2)
    log("reconcile: stale lock from a dead session - clearing, TV untouched")
    LOCK.unlink(missing_ok=True)
    return 0


def usage():
    print("usage: couch.py [start [appid]|reconcile]")
    return 2


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        if len(sys.argv) > 2 and not sys.argv[2].isdigit():
            sys.exit(usage())               # a non-digit appid is a caller bug
        appid = sys.argv[2] if len(sys.argv) > 2 else None
        sys.exit(start(appid))
    elif cmd == "reconcile":
        sys.exit(reconcile())
    else:
        sys.exit(usage())
