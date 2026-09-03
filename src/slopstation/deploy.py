"""CD's K15 leg: land one commit on this checkout without ending a session.

Runs from the LIVE checkout, never a runner workspace: paths.HOME is what
locates the session lock this gates on and the rev doctor.py compares with the
gaming PC's build-id, so a workspace copy would gate on the wrong lock and
leave the real checkout behind.

    python -m slopstation.deploy --sha <sha> [--wait-minutes 120]

Exit code = doctor.py's (its FAIL count); 1 if the deploy could not finish.
The reload is `schtasks /End` then `/Run` on each lane's task. The scheduler
starts a task in the session it was registered for - the logged-on user's,
where the Puck and the audio devices are - whichever process asks, so the
runner can bring the chord lane back even when it was down.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from slopstation import cglib, paths, supervise

log = cglib.make_log("deploy")

ROOT = paths.HOME

IDLE_POLL_S = 15
RELAUNCH_S = 90  # a /Run is near-instant; this is room for a slow box
# A relaunch that has to pip install first. The supervisor calls it "a minute
# or two"; a cold venv on this box is longer, and this is a ceiling that only
# elapses when something is genuinely stuck, not a wait that is spent.
REINSTALL_S = 900

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


def wait_up(want: set[str], installed: bool, budget_s: float) -> set[str]:
    """The lanes whose task is not Running once the budget is spent. When the
    pins changed, the lane is also not counted up until its wrapper has
    written the sentinel: Running alone would be pip, not the lane."""
    deadline = time.time() + budget_s
    while True:
        missing = {lane for lane in want if not supervise.running(lane)}
        if installed and supervise.pins_changed():
            missing = set(want)
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

        was = {lane for lane in supervise.LANES if supervise.running(lane)}
        for lane in sorted(was):
            log("deploy_reloaded", what=lane, killed=int(supervise.stop(lane)))
        # The chord lane is required back whether or not it was up to begin
        # with: a deploy that leaves it dead has landed code that nothing is
        # running, and doctor.py only WARNs about that. Voice is an overlay:
        # it comes back only if it was up, so a lane someone stopped on
        # purpose stays stopped.
        want = was | {"listener"}
        for lane in sorted(want):
            supervise.run(lane)
        reinstall = supervise.pins_changed()
        budget = REINSTALL_S if reinstall else RELAUNCH_S
        if reinstall:
            print(
                "pins changed - the lane installs them before it comes up; "
                f"waiting up to {budget / 60:.0f} min",
                flush=True,
            )
        missing = wait_up(want, reinstall, budget)
        if missing:
            raise RuntimeError(
                f"{', '.join(sorted(missing))} not running {budget:.0f}s after the "
                "reload - task not registered? run Setup-K15-Tasks.ps1 there"
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
