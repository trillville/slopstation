"""Run Dispatch.ps1 commands on the gaming PC over SSH."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime

from slopstation import config, events


def ssh(cmd: str, timeout: float = 15) -> str:
    """Run one Dispatch verb on the host; returns its stdout.

    ``check=True`` prevents SSH errors from being mistaken for session state.
    """
    r = subprocess.run(
        ["ssh", config.current()["sshHost"], cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return r.stdout.strip()


def ssh_intent(cmd: str, turn: str | None = None, **kw) -> str:
    """A MUTATING verb, tagged with this launch's turn id; read-only polls use
    plain ssh(). Pass `turn` explicitly from callers whose ambient context
    predates the utterance (the voice lane's - a ContextVar cannot reach it)."""
    turn = turn or events.current().get("turn")
    # Dispatch fails CLOSED on a malformed id (matches no verb, answers
    # DENIED), so re-validate here and send it uncorrelated instead.
    return ssh(f"{cmd} --turn {turn}" if events.valid_turn(turn) else cmd, **kw)


def enter_running() -> bool | None:
    """Whether the PC's Enter task is still running, or None when it could not
    say (a PC predating `enterstate` answers DENIED; an ssh blip raises).
    Re-dispatching on a None would fight a healthy Enter."""
    try:
        ans = enterstate()
    except Exception:
        return None
    if ans == "RUNNING":
        return True
    # NOTASK is unreachable in practice but is still not-running.
    if ans in ("IDLE", "NOTASK"):
        return False
    return None


# --- the verbs ---------------------------------------------------------------
# Read-only polls use ssh(); the five mutating verbs ride ssh_intent() with the
# turn. Each returns Dispatch's answer as printed.


def enter(turn: str | None = None) -> str:
    return ssh_intent("enter", turn)


def exit(turn: str | None = None) -> str:
    return ssh_intent("exit", turn)


def status() -> str:
    answer = ssh("status")
    if answer != "NOTREADY" and not events.valid_turn(answer):
        # Manual Enter tasks and older deployments write an ISO timestamp.
        try:
            if "T" not in answer:
                raise ValueError
            datetime.fromisoformat(answer)
        except ValueError:
            raise ValueError(f"invalid gaming-PC status: {answer!r}") from None
    return answer


def enterstate() -> str:
    return ssh("enterstate")


def version() -> str:
    return ssh("version")


def playing() -> str:
    return ssh("playing")


def games() -> str:
    return ssh("games", timeout=30)


def collections() -> str:
    return ssh("collections", timeout=15)


def launch(appid: int | str, turn: str | None = None) -> str:
    return ssh_intent(f"launch {int(appid)}", turn)


def stop(appid: int | str, turn: str | None = None) -> str:
    return ssh_intent(f"stop {int(appid)}", turn)


def nav(kind: str, arg: object = None, turn: str | None = None) -> str:
    return ssh_intent(nav_cmd(kind, arg), turn)


def disk() -> str:
    """Free space on each Steam library drive, as JSON rows."""
    return ssh("disk", timeout=15)


def sleep(turn: str | None = None) -> str:
    """Put the PC to sleep; Dispatch refuses (BUSY) while a session is live."""
    return ssh_intent("sleep", turn)


def nav_cmd(kind: str, arg: object = None) -> str:
    return f"nav {kind}" + (f" {arg}" if arg not in (None, "") else "")


# The PC's `nav url` allowlist, character for character: two Steam hosts, a
# bounded URL charset with no whitespace (so `--turn` can never be absorbed).
# test_gaming_pc_scripts holds the three copies (here, Dispatch.ps1,
# Nav-BigPicture.ps1) equal.
NAV_URL_PATTERN = (
    r"https://(?:store\.steampowered\.com|steamcommunity\.com)/"
    r"[A-Za-z0-9/_.~?=&%+-]{1,300}"
)
NAV_URL_RE = re.compile(NAV_URL_PATTERN)


# The verb surface, one name per Dispatch.ps1 switch arm (test_turn compares).
VERBS = (
    "enter",
    "exit",
    "status",
    "enterstate",
    "version",
    "playing",
    "games",
    "collections",
    "launch",
    "stop",
    "nav",
    "disk",
    "sleep",
)

# Answers: OK NOTREADY ALREADY NOTRUNNING NOTINSTALLED RUNNING IDLE BUSY DENIED,
# and BUSY:<appid> NOTASK:<name> FAILED:<code> with an argument after the colon.
