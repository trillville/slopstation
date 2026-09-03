"""The lanes as scheduled tasks: what each one runs, and the verbs the deployer,
the doctor and Start-Slopstation.bat use on them.

Task Scheduler owns the lifecycle - start at logon, one instance at a time, in
the logged-on session where the Puck and the audio devices are - and
Setup-K15-Tasks.ps1 declares it. Each task's action is

    slopstation-lane <name>

the wrapper below: install changed pins, run the lane, and run it again after
a crash. The scheduler's own restart-on-failure does not fire on a non-zero
exit (measured), so the wrapper carries that. A Windows service cannot host
these lanes: session 0 reaches neither device.

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
from ctypes import wintypes
from pathlib import Path

from slopstation import logbook, paths

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
RESTART_S = (
    10  # a crashed lane is back this soon; a crash LOOP is one restart every RESTART_S
)

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
    # schtasks localises the Status text; this rig is English.
    return (query(lane) or {}).get("Status") == "Running"


def stop(lane: str) -> bool:
    """End the task and wait for the scheduler to agree it has stopped. True
    when there was something to stop; raises when it did not stop in time.
    Never a quiet True: a caller that went on to /Run would be refused as a
    second instance (IgnoreNew) and then take the OLD instance for its
    relaunch - a deploy that reports success with the old code running."""
    if not running(lane):
        return False
    _schtasks("/End", "/TN", TASKS[lane])
    deadline = time.time() + STOP_WAIT_S
    while running(lane):
        if time.time() >= deadline:
            raise RuntimeError(
                f"{TASKS[lane]} is still running {STOP_WAIT_S}s after schtasks /End"
            )
        time.sleep(0.5)
    return True


def run(lane: str) -> None:
    """Start the task, or raise. The scheduler starts it in the session it was
    registered for, whoever asks - which is what lets the deployer bring a
    lane back."""
    r = _schtasks("/Run", "/TN", TASKS[lane])
    if r.returncode:
        raise RuntimeError(
            f"schtasks /Run {TASKS[lane]} failed: {(r.stderr or r.stdout).strip()}"
        )


# --- the job object: end the wrapper, end everything it started ------------------
_JOB_KILL_ON_CLOSE = 0x2000
_JOB_EXTENDED_LIMITS = 9


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# Typed prototypes, or GetCurrentProcess's pseudo-handle (-1) comes back as a
# 32-bit int and AssignProcessToJobObject sees an invalid handle.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetCurrentProcess.argtypes = []
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _kill_on_close_job() -> int:
    """A job object whose processes all die when its last handle closes."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
    ok = _k32.SetInformationJobObject(
        job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return job


_job = None  # the wrapper's handle: held for its lifetime, never closed


def _die_together() -> None:
    """Put THIS process in a kill-on-close job, so every process it starts - the
    lane, and the interpreter the venv launcher spawns for it - dies with it.
    Ending the task is TerminateProcess on the wrapper, which closes its handles
    and so the job. Measured 2026-09-03: the scheduler does not do this itself,
    and /End left the lane running as an orphan beside its replacement."""
    global _job
    job = _kill_on_close_job()
    if not _k32.AssignProcessToJobObject(job, _k32.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    _job = job


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
    marker = paths.state() / "listener.boot"
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
    """Run one lane, and run it again RESTART_S after any crash, for as long as
    the task lives. A clean exit (--once, Ctrl-C) ends the task.

    The wrapper owns crash recovery because the scheduler does not: measured
    2026-09-03, a task whose action exits non-zero is left at Ready, whether
    the run was started by /Run or by a trigger. What the scheduler is good
    for - starting at logon in the interactive session, a name to /End and
    /Run, one instance at a time - it keeps."""
    if elevated():
        print(f"[lane] {REFUSAL}")
        return 1
    _die_together()
    log = logbook.logger("supervisor")
    if name == "listener" and _first_launch_this_boot():
        # Once per boot, before the listener: re-running it after a mid-session
        # crash would spawn a second watch loop against the live session lock.
        subprocess.run(
            [sys.executable, "-m", "slopstation.couch", "reconcile"], cwd=paths.HOME
        )
    while True:
        # Inside the loop: a deploy lands new pins and then ends the task, but
        # a crash-restart must pick them up too.
        _install_if_pins_changed(log)
        log("start", what=name)
        code = subprocess.run(LANES[name] + passthrough, cwd=paths.HOME).returncode
        if code == 0:
            return 0
        print(f"[lane] {name} exited (code {code}) - restarting in {RESTART_S}s")
        log.warn("restart", what=name, code=code)
        time.sleep(RESTART_S)


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
    log = logbook.logger("supervisor")
    for name in LANES:
        info = query(name)
        if info is None:
            print(
                f"[start] {name}: {TASKS[name]} is not registered - run Setup-K15-Tasks.ps1"
            )
            return 1
        try:
            if info.get("Status") == "Running":
                stop(name)
                run(name)
                print(f"[start] {name}: reloaded")
                log("lane_reloaded", what=name, killed=1)
            else:
                run(name)
                print(f"[start] {name}: started")
                log("lane_started", what=name)
        except RuntimeError as e:
            print(f"[start] {name}: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(lane_main())
