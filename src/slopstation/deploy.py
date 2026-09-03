"""CD's K15 leg: land one commit on this checkout without ending a session.

Runs from the LIVE checkout, never a runner workspace: paths.HOME is what
locates the session lock this gates on and the rev doctor.py compares with the
gaming PC's build-id, so a workspace copy would gate on the wrong lock and
leave the real checkout behind.

    python -m slopstation.deploy --sha <sha> [--wait-minutes 120]

Exit code = doctor.py's (its FAIL count); 1 if the deploy could not finish.
The reload is kill-and-let-the-supervisor-relaunch, never a start: a runner
outside the interactive session would put a started lane in session 0, where it
reaches neither the Puck nor the audio devices. An agent that does not come
back is therefore the failure, not something to start from here.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

from slopstation import cglib, paths, supervise

log = cglib.make_log("deploy")

ROOT = paths.HOME

IDLE_POLL_S = 15
RELAUNCH_S = 90  # supervisor backoff is 10 s; this is that with room
# A relaunch that has to pip install first. The supervisor calls it "a minute
# or two"; a cold venv on this box is longer, and this is a ceiling that only
# elapses when something is genuinely stuck, not a wait that is spent.
REINSTALL_S = 900

# The supervisor's dependency gate, read rather than guessed: it hashes the
# pin files and compares that to a sentinel in the venv, inside its restart
# loop, so killing the agent is what makes it fire. The sentinel is written
# only after pip succeeds, so a failed install leaves the reinstall pending
# too - which a diff of the commits being landed would not show.
DEPS_OK = pathlib.Path(sys.prefix) / "deps-ok"

# Compose reads compose.yaml only when something runs `up`, so a landed edit to
# it changes nothing until this script runs - the containers keep the shape
# they were created with. `up -d` is a no-op when nothing changed, so this runs
# every deploy rather than diffing the commits.
MEDIA_SCRIPT = ROOT / "media" / "Start-Media.ps1"
MEDIA_S = 900  # a compose up that has to pull an image first


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=180
    )
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def media_enabled() -> bool:
    """config.json's media.enabled. deploy.py reads the file for nothing else,
    so an unreadable one means no stack here, not a failed deploy."""
    try:
        media = cglib.config().get("media")
    except (OSError, ValueError):
        return False
    return bool(isinstance(media, dict) and media.get("enabled"))


def start_media() -> None:
    """Bring the media stack onto the compose file just landed. Fails the
    deploy: it only runs where the stack is enabled, and a stack that will not
    come up is what this step exists to make visible - doctor.py's media rows
    are WARNs, so nothing else here goes red."""
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(MEDIA_SCRIPT),
        ],
        capture_output=True,
        text=True,
        timeout=MEDIA_S,
    )
    if r.returncode:
        tail = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(
            "media stack did not come up: "
            + (tail[-1] if tail else f"exit {r.returncode}")
        )


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
            log.warn("deploy_deferred", reason="session_active", budget_s=int(budget_s))
            print(
                f"a session is active - waiting up to {budget_s / 60:.0f} min",
                flush=True,
            )
            announced = True
        time.sleep(IDLE_POLL_S)


def reinstall_pending() -> bool:
    """True when the supervisor will pip install before it relaunches its
    lane, so the relaunch wait has to cover that too."""
    try:
        return supervise._pin_digest() != DEPS_OK.read_text().strip()
    except OSError:
        return True  # no venv, or no sentinel: the gate fires


def wait_fresh(
    want: set[str], killed: dict[str, set[int]], budget_s: float
) -> set[str]:
    """The lanes with no live pid OTHER than the one we just killed. Measured
    2026-08-30: Stop-Process returns while the corpse is still in the process
    table, so a name-only check read it as the replacement and passed ~10 s
    before the supervisor had relaunched anything - which is the entire window
    this is here to watch."""
    deadline = time.time() + budget_s
    while True:
        live = supervise.pids()
        missing = {lane for lane in want if not (live[lane] - killed.get(lane, set()))}
        if not missing or time.time() >= deadline:
            return missing
        time.sleep(5)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="land one commit on this checkout")
    ap.add_argument("--sha", required=True, help="the commit to land")
    ap.add_argument(
        "--wait-minutes",
        type=float,
        default=120.0,
        help="how long to defer while a session is live",
    )
    a = ap.parse_args(argv)

    t0 = time.time()
    log("deploy_start", sha=a.sha[:12], at=str(ROOT))
    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            raise RuntimeError(f"checkout is on {branch!r}, not main")
        # Tracked files only: the K15 carries untracked wake-training wavs and
        # candidate models by design, and none of them stop a fast-forward. A
        # collision between one and an incoming file fails the merge below, on
        # its own terms.
        if git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("checkout has uncommitted changes to tracked files")

        if not wait_idle(a.wait_minutes * 60):
            log.warn(
                "deploy_deferred", reason="gave_up", waited_s=int(time.time() - t0)
            )
            print("DEFERRED: a session is still active - nothing landed")
            return 1

        git("fetch", "origin", "main")
        before = git("rev-parse", "--short", "HEAD")
        # --ff-only: a checkout that has diverged is a person's business, not a
        # deployer's. An already-landed sha is a no-op, not a rewind.
        git("merge", "--ff-only", a.sha)
        after = git("rev-parse", "--short", "HEAD")
        print(f"checkout {before} -> {after}", flush=True)

        pids_before = supervise.pids()
        was = {lane for lane, pids in pids_before.items() if pids}
        for lane in sorted(was):
            log("deploy_reloaded", what=lane, killed=supervise.kill(lane))
        # The chord lane is required back whether or not it was up to begin
        # with: a deploy that leaves it dead has landed code that nothing is
        # running, and doctor.py only WARNs about that, so this is the only
        # thing standing between a dead chord lane and a green CD run. Voice is
        # an overlay and may stay off.
        reinstall = reinstall_pending()
        budget = REINSTALL_S if reinstall else RELAUNCH_S
        if reinstall:
            print(
                "pins changed - the supervisor pip installs before the "
                f"agent comes back; waiting up to {budget / 60:.0f} min",
                flush=True,
            )
        missing = wait_fresh(was | {"listener"}, pids_before, budget)
        if missing:
            raise RuntimeError(
                f"{', '.join(sorted(missing))} not running {budget:.0f}s after the "
                "reload - no supervisor to relaunch it? run Start-Slopstation.bat there"
            )

        if media_enabled():
            t_media = time.time()
            start_media()
            log("deploy_media", dur_ms=int((time.time() - t_media) * 1000))

        fails = subprocess.run(
            [sys.executable, "-m", "slopstation.doctor"], cwd=str(ROOT), timeout=900
        ).returncode
        log(
            "deploy_done",
            sha=after,
            was=before,
            fails=fails,
            reinstall=int(reinstall),
            dur_ms=int((time.time() - t0) * 1000),
        )
        return fails
    except Exception as e:
        log.error("deploy_failed", err=str(e), sha=a.sha[:12])
        print(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
