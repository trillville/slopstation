"""Keeps a lane running: start it, restart it after any exit, and emit the
transitions so a crash LOOP is alertable.

    slopstation-supervise listener     one lane, in this console
    slopstation-start                  both lanes, or reload them onto new code

Single instance per lane is a byte lock held for the supervising process's
lifetime. Windows drops it when the holder dies, so a killed supervisor cannot
wedge the next one.
"""

import argparse
import hashlib
import msvcrt
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from slopstation import cglib, paths

RESTART_S = 10

# What each lane runs. Module invocations, not paths: the package is installed.
LANES = {
    "listener": [sys.executable, "-m", "slopstation.chord_listener"],
    "voice": [sys.executable, "-m", "slopstation.agent.voice_agent"],
}

# Bumping a pin has to install itself on the next launch: CD pulls code, not
# wheels.
PINS = ("pyproject.toml", "constraints.txt")
# Written only after pip succeeds, so a half-built venv retries next launch;
# the .lock beside it serialises installers.
SENTINEL = Path(sys.prefix) / "deps-ok"

# Process control by lane, shared with deploy.py and doctor.py. Filtered to
# python* so the PowerShell doing the filtering - whose own command line holds
# the needle - cannot match itself.
_PIDS_PS = (
    "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
    '| ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }'
)
_KILL_PS = (
    "$p = @(Get-CimInstance Win32_Process | Where-Object "
    "{{ $_.Name -like 'python*' -and $_.CommandLine -like '*{needle}*' }}); "
    "$p | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}; "
    "exit $p.Count"
)


def pids() -> dict[str, set[int]]:
    """lane -> the pids of the pythons running it. Pids rather than a yes/no:
    Stop-Process returns before the process leaves the table, so a caller
    waiting for a RELAUNCH has to tell the corpse from its replacement."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _PIDS_PS],
        capture_output=True,
        text=True,
        timeout=30,
    )
    out: dict[str, set[int]] = {lane: set() for lane in LANES}
    for line in (r.stdout or "").splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        for lane, argv in LANES.items():
            if argv[-1] in cmd:
                out[lane].add(int(pid))
    return out


def kill(lane: str) -> int:
    """Stop the lane's PROCESS, never its supervisor: the supervisor relaunches
    it on the next loop, which is what makes this a reload. Returns how many
    were stopped."""
    command = _KILL_PS.format(needle=LANES[lane][-1])
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return r.returncode


def _lock_path(lane):
    return Path(tempfile.gettempdir()) / f"slopstation-{lane}.lock"


def _hold(lane):
    """The single-instance lock, or None when another supervisor holds it."""
    handle = open(_lock_path(lane), "w")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _supervised(lane):
    """True when a supervisor already holds this lane's lock."""
    handle = _hold(lane)
    if handle is None:
        return True
    handle.close()
    return False


def _pin_digest():
    h = hashlib.sha256()
    for name in PINS:
        try:
            h.update((paths.HOME / name).read_bytes())
        except OSError:
            return None
    return h.hexdigest()


def pins_changed() -> bool:
    """True when the pins differ from what the venv last installed."""
    digest = _pin_digest()
    if digest is None:
        return False  # no pin files to compare: nothing to install from
    try:
        return digest != SENTINEL.read_text().strip()
    except OSError:
        return True  # no sentinel: never installed, or the install failed


def _install_if_pins_changed(log):
    """One installer at a time: both supervisors start together, and two pips
    in one venv corrupt it. The second waits on the lock, then re-reads the
    sentinel the first wrote and skips."""
    if not pins_changed():
        return
    with open(SENTINEL.with_name("deps.lock"), "w") as lock:
        while True:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(1)
        try:
            if not pins_changed():
                return
            print("[supervisor] pins changed - installing, takes a minute or two...")
            pip = [sys.executable, "-m", "pip", "install", "-e", ".[dev]"]
            r = subprocess.run([*pip, "-c", "constraints.txt"], cwd=paths.HOME)
            if r.returncode:
                print("[supervisor] pip install failed - fix it and relaunch")
                return
            SENTINEL.write_text(_pin_digest() or "")
            log("deps_installed", what="venv")
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def supervise(lane, passthrough):
    """One lane, restarted forever. Returns an exit code only when it could
    not start at all."""
    log = cglib.make_log("supervisor")
    handle = _hold(lane)
    if handle is None:
        print(f"[supervisor] another {lane} supervisor is running - close it first")
        return 1
    if lane == "listener":
        # Once per boot, OUTSIDE the loop: re-running it mid-session would
        # spawn a second watch loop against the live session lock.
        subprocess.run(
            [sys.executable, "-m", "slopstation.couch", "reconcile"], cwd=paths.HOME
        )
    log("start", what=lane)
    while True:
        # Inside the loop: a deploy lands new pins and then kills the lane,
        # and the relaunch is where they have to be installed.
        _install_if_pins_changed(log)
        code = subprocess.run(LANES[lane] + passthrough, cwd=paths.HOME).returncode
        print(f"[supervisor] {lane} exited (code {code}) - restarting in {RESTART_S}s")
        log.warn("restart", what=lane, code=code)
        time.sleep(RESTART_S)


def start():
    """Both lanes on the code on disk right now: start a supervisor that is
    down, reload one that is up. The Windows startup shortcut and the thing to
    run after a git pull."""
    log = cglib.make_log("supervisor")
    reloaded = False
    for lane in LANES:
        if _supervised(lane):
            killed = kill(lane)
            reloaded = True
            print(
                f"[start] {lane}: {'stopped' if killed else 'already down'}"
                " - its supervisor will bring it back"
            )
            log("lane_reloaded", what=lane, killed=killed)
        else:
            print(f"[start] {lane} down - starting it")
            log("lane_started", what=lane)
            subprocess.Popen(
                [sys.executable, "-m", "slopstation.supervise", lane],
                cwd=paths.HOME,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
    if reloaded:
        print(
            f"\n[start] reloaded - each supervisor relaunches its lane "
            f"within ~{RESTART_S}s."
        )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "lane", choices=sorted(LANES), help="which lane to supervise in this console"
    )
    args, passthrough = ap.parse_known_args(argv)
    return supervise(args.lane, passthrough)


if __name__ == "__main__":
    sys.exit(main())
