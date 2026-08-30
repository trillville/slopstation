"""CD's K15 leg: land one commit on this checkout without ending a session.

Runs from the LIVE checkout, never a runner workspace: cglib.BASE is what
locates the session lock this gates on and the rev doctor.py compares with the
gaming PC's build-id, so a workspace copy would gate on the wrong lock and
leave the real checkout behind.

    python deploy.py --sha <sha> [--wait-minutes 120]

Exit code = doctor.py's (its FAIL count); 1 if the deploy could not finish.
The reload is kill-and-let-the-supervisor-relaunch, never a start: a runner
outside the interactive session would put a started lane in session 0, where it
reaches neither the Puck nor the audio devices. An agent that does not come
back is therefore the failure, not something to start from here.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import cglib

log = cglib.make_log("deploy")

ROOT = cglib.BASE.parent

# Command-line needle -> lane name. The same needles Start-K15.bat kills on.
AGENTS = {"chord_listener.py": "listener", "voice_agent.py": "voice"}
LISTENER = "chord_listener.py"

IDLE_POLL_S = 15
RELAUNCH_S = 90         # supervisor backoff is 10 s; this is that with room

# Name-filtered to python* so the powershell doing the filtering - its own
# command line contains the needle - cannot match itself.
_KILL_PS = ("$p = @(Get-CimInstance Win32_Process | Where-Object "
            "{ $_.Name -like 'python*' -and $_.CommandLine -like '*%s*' }); "
            "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
            "exit $p.Count")

_CMDLINES_PS = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
                "| Select-Object -ExpandProperty CommandLine")


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args],
                       capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def running_agents() -> set[str]:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", _CMDLINES_PS],
                       capture_output=True, text=True, timeout=30)
    return {needle for needle in AGENTS if needle in (r.stdout or "")}


def kill(needle: str) -> int:
    """Stop the agent whose command line contains `needle`; its supervisor
    relaunches it. Returns how many were killed."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", _KILL_PS % needle],
                       capture_output=True, text=True, timeout=60)
    return r.returncode


def wait_idle(budget_s: float) -> bool:
    """True once no launch or session owns the K15. Copying over a live one
    swaps code out from under a watch loop with someone on the couch."""
    deadline = time.time() + budget_s
    announced = False
    while True:
        if not cglib.session_active():
            return True
        if time.time() >= deadline:
            return False
        if not announced:
            log.warn("deploy_deferred", reason="session_active",
                     budget_s=int(budget_s))
            print(f"a session is active - waiting up to {budget_s / 60:.0f} min",
                  flush=True)
            announced = True
        time.sleep(IDLE_POLL_S)


def wait_relaunch(want: set[str]) -> set[str]:
    """The agents that did NOT come back. Only the ones that were up before the
    reload are waited for: the voice overlay is allowed to be off."""
    deadline = time.time() + RELAUNCH_S
    while True:
        missing = want - running_agents()
        if not missing or time.time() >= deadline:
            return missing
        time.sleep(5)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="land one commit on this checkout")
    ap.add_argument("--sha", required=True, help="the commit to land")
    ap.add_argument("--wait-minutes", type=float, default=120.0,
                    help="how long to defer while a session is live")
    a = ap.parse_args(argv)

    t0 = time.time()
    log("deploy_start", sha=a.sha[:12], at=str(ROOT))
    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            raise RuntimeError(f"checkout is on {branch!r}, not main")
        if git("status", "--porcelain"):
            raise RuntimeError("checkout has uncommitted changes")

        if not wait_idle(a.wait_minutes * 60):
            log.warn("deploy_deferred", reason="gave_up",
                     waited_s=int(time.time() - t0))
            print("DEFERRED: a session is still active - nothing landed")
            return 1

        git("fetch", "origin", "main")
        before = git("rev-parse", "--short", "HEAD")
        # --ff-only: a checkout that has diverged is a person's business, not a
        # deployer's. An already-landed sha is a no-op, not a rewind.
        git("merge", "--ff-only", a.sha)
        after = git("rev-parse", "--short", "HEAD")
        print(f"checkout {before} -> {after}", flush=True)

        was = running_agents()
        for needle in sorted(was):
            log("deploy_reloaded", what=AGENTS[needle], killed=kill(needle))
        # The chord lane is required back whether or not it was up to begin
        # with: a deploy that leaves it dead has landed code that nothing is
        # running, and doctor.py only WARNs about that, so this is the only
        # thing standing between a dead chord lane and a green CD run. Voice is
        # an overlay and may stay off.
        missing = wait_relaunch(was | {LISTENER})
        if missing:
            lanes = ", ".join(sorted(AGENTS[m] for m in missing))
            raise RuntimeError(f"{lanes} not running {RELAUNCH_S}s after the "
                               "reload - no supervisor to relaunch it? run "
                               "Start-K15.bat there")

        fails = subprocess.run([sys.executable, "doctor.py"],
                               cwd=str(cglib.BASE), timeout=900).returncode
        log("deploy_done", sha=after, was=before, fails=fails,
            dur_ms=int((time.time() - t0) * 1000))
        return fails
    except Exception as e:
        log.error("deploy_failed", err=str(e), sha=a.sha[:12])
        print(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
