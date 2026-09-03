"""The lanes as scheduled tasks: what each one runs, and the verbs the deployer,
the doctor and Start-Slopstation.bat use on them.

Task Scheduler owns the lifecycle - start at logon, restart on failure, one
instance at a time, in the logged-on session where the Puck and the audio
devices are - and Setup-K15-Tasks.ps1 declares it. Each task's action is

    slopstation-lane <name>

the wrapper below: install changed pins, run the lane, return its exit code so
a crash reads as a failed task and the scheduler relaunches it. A Windows
service cannot host these lanes: session 0 reaches neither device.

    slopstation-start          start what is down, reload what is up
"""

import argparse
import csv
import ctypes
import hashlib
import io
import msvcrt
import subprocess
import sys
import time
from pathlib import Path

from slopstation import cglib, paths

# What each lane runs. Module invocations, not paths: the package is installed.
LANES = {
    "listener": [sys.executable, "-m", "slopstation.chord_listener"],
    "voice": [sys.executable, "-m", "slopstation.agent.voice_agent"],
}
TASKS = {lane: f"\\Slopstation\\{lane}" for lane in LANES}

# Bumping a pin has to install itself on the next launch: CD pulls code, not
# wheels.
PINS = ("pyproject.toml", "constraints.txt")
# Written only after pip succeeds, so a half-built venv retries next launch;
# the .lock beside it serialises installers.
SENTINEL = Path(sys.prefix) / "deps-ok"

STOP_WAIT_S = 15

REFUSAL = (
    "refusing to run elevated: a lane started from an administrator window "
    "cannot be stopped, or even seen, from a normal one - and the deployer "
    "runs in a normal one. Use a normal window."
)


def elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# --- the tasks ----------------------------------------------------------------


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, timeout=30
    )


def query(lane: str) -> dict[str, str] | None:
    """The task's verbose row as a dict (Status, Last Result, Last Run Time,
    ...), or None when it is not registered."""
    r = _schtasks("/Query", "/TN", TASKS[lane], "/FO", "CSV", "/V")
    if r.returncode:
        return None
    rows = list(csv.reader(io.StringIO(r.stdout)))
    if len(rows) < 2:
        return None
    return dict(zip(rows[0], rows[1], strict=False))


def running(lane: str) -> bool:
    return (query(lane) or {}).get("Status") == "Running"


def stop(lane: str) -> bool:
    """End the task and wait for the scheduler to agree it has stopped, so a
    /Run straight after is not refused as a second instance. True when there
    was something to stop."""
    if not running(lane):
        return False
    _schtasks("/End", "/TN", TASKS[lane])
    deadline = time.time() + STOP_WAIT_S
    while running(lane) and time.time() < deadline:
        time.sleep(0.5)
    return True


def run(lane: str) -> bool:
    """Start the task. The scheduler starts it in the session it was
    registered for, whoever asks - which is what lets the deployer bring a
    lane back."""
    return _schtasks("/Run", "/TN", TASKS[lane]).returncode == 0


# --- the wrapper the tasks run --------------------------------------------------


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
    """One installer at a time: both tasks start at logon together, and two
    pips in one venv corrupt it. The second waits on the lock, then re-reads
    the sentinel the first wrote and skips."""
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
            print("[lane] pins changed - installing, takes a minute or two...")
            pip = [sys.executable, "-m", "pip", "install", "-e", ".[dev]"]
            r = subprocess.run([*pip, "-c", "constraints.txt"], cwd=paths.HOME)
            if r.returncode:
                print("[lane] pip install failed - fix it and relaunch")
                return
            SENTINEL.write_text(_pin_digest() or "")
            log("deps_installed", what="venv")
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _uptime_s() -> float:
    return ctypes.windll.kernel32.GetTickCount64() / 1000


def _first_launch_this_boot() -> bool:
    """True once per boot. The marker holds the boot's epoch; a launch that
    reads the same boot back is the scheduler restarting a crashed lane, not a
    boot."""
    marker = cglib.STATE / "listener.boot"
    boot = round(time.time() - _uptime_s())
    try:
        if abs(int(marker.read_text()) - boot) < 30:
            return False
    except (OSError, ValueError):
        pass
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(boot))
    return True


def lane(name: str, passthrough: list[str]) -> int:
    """Run one lane once and return its exit code. The scheduler restarts a
    task whose action failed, so a crash comes back on its interval; a clean
    exit (--once) stays down."""
    if elevated():
        print(f"[lane] {REFUSAL}")
        return 1
    log = cglib.make_log("supervisor")
    _install_if_pins_changed(log)
    if name == "listener" and _first_launch_this_boot():
        # Once per boot, before the listener: re-running it after a mid-session
        # crash would spawn a second watch loop against the live session lock.
        subprocess.run(
            [sys.executable, "-m", "slopstation.couch", "reconcile"], cwd=paths.HOME
        )
    log("start", what=name)
    code = subprocess.run(LANES[name] + passthrough, cwd=paths.HOME).returncode
    if code:
        # The scheduler relaunches a failed task; this is the alertable trace
        # of a crash loop.
        log.warn("restart", what=name, code=code)
    return code


def lane_main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("lane", choices=sorted(LANES))
    args, passthrough = ap.parse_known_args(argv)
    return lane(args.lane, passthrough)


# --- Start-Slopstation.bat -------------------------------------------------------


def start() -> int:
    """Both lanes on the code on disk right now: start a task that is down,
    end and re-run one that is up. The thing to run after a git pull."""
    if elevated():
        print(f"[start] {REFUSAL}")
        return 1
    log = cglib.make_log("supervisor")
    for name in LANES:
        info = query(name)
        if info is None:
            print(
                f"[start] {name}: {TASKS[name]} is not registered - run Setup-K15-Tasks.ps1"
            )
            return 1
        if info.get("Status") == "Running":
            stop(name)
            run(name)
            print(f"[start] {name}: reloaded")
            log("lane_reloaded", what=name, killed=1)
        else:
            run(name)
            print(f"[start] {name}: started")
            log("lane_started", what=name)
    return 0


if __name__ == "__main__":
    sys.exit(lane_main())
