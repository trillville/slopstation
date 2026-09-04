"""Deploy one commit to the K15 without interrupting an active session.

Run this from the live checkout so it uses the active session lock.

    python -m slopstation.deploy --sha <sha> [--wait-minutes 120]

The exit code is doctor.py's failure count, or 1 if deployment cannot finish.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from slopstation import config, logbook, paths, sessionlock, supervise

log = logbook.logger("deploy")

IDLE_POLL_S = 15
RELAUNCH_S = 90  # a /Run is near-instant; this is room for a slow box
REINSTALL_S = 900  # a relaunch that has to pip install first: a ceiling, not a wait
MEDIA_S = 900  # a compose up that has to pull an image first


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(paths.HOME), *args],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def media_enabled() -> bool:
    """config.json's media.enabled. deploy.py reads the file for nothing else,
    so an unreadable one means no stack here, not a failed deploy."""
    try:
        media = config.current().get("media")
    except (OSError, ValueError):
        return False
    return bool(isinstance(media, dict) and media.get("enabled"))


def start_media() -> None:
    """Bring the media stack onto the compose file just landed: containers keep
    the compose file they were created with until something runs `up`. A
    stack that will not come up fails the deploy - doctor.py's media rows are
    WARNs, so nothing else would go red."""
    script = paths.HOME / "media" / "Start-Media.ps1"
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
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
        if not sessionlock.active():
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
    log("deploy_start", sha=a.sha[:12], at=str(paths.HOME))
    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            raise RuntimeError(f"checkout is on {branch!r}, not main")
        # Tracked files only: the K15 carries untracked wake-training data by
        # design, and a collision with an incoming file fails the merge itself.
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
        # The chord lane comes back whether or not it was up: a deploy that
        # leaves it dead has landed code nothing runs, and doctor.py only WARNs
        # about that. Voice comes back only if it was up, so a lane someone
        # stopped on purpose stays stopped.
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
            [sys.executable, "-m", "slopstation.doctor"], cwd=paths.HOME, timeout=900
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
