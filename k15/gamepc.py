"""The gaming PC as the K15 sees it: one ssh call per Dispatch.ps1 verb, the
turn tag on the mutating ones, and the answer words. Dispatch.ps1 is the
server; agent/tests/test_turn.py holds the two in step.
"""
from __future__ import annotations

import subprocess

import cglib
import events


def ssh(cmd: str, timeout: float = 15) -> str:
    """Run one Dispatch verb on the host; returns its stdout.

    stdout only, so stderr noise stays out of state comparisons; Dispatch
    reports its own failures as FAILED:<code>. check=True is load-bearing: an
    unreachable host RAISES instead of returning ssh's error text as session
    state - otherwise the READY poll reads 'ssh: connect ... timed out' as
    READY, and watch() never detects sleep."""
    r = subprocess.run(["ssh", cglib.config()["sshHost"], cmd],
                       capture_output=True, text=True, timeout=timeout, check=True)
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
    """True/False if the gaming PC could tell us whether its Enter task is
    still running; None if it could not. The None is load-bearing: a PC
    predating `enterstate` answers DENIED and an ssh blip raises, and
    re-dispatching on either would fight a healthy Enter."""
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
    return ssh("status")


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


def nav_cmd(kind: str, arg: object = None) -> str:
    return f"nav {kind}" + (f" {arg}" if arg not in (None, "") else "")


# The verb surface, one name per Dispatch.ps1 switch arm (test_turn compares).
VERBS = ("enter", "exit", "status", "enterstate", "version", "playing", "games",
         "collections", "launch", "stop", "nav")

# Answers: OK NOTREADY ALREADY NOTRUNNING NOTINSTALLED RUNNING IDLE DENIED, and
# BUSY:<appid> NOTASK:<name> FAILED:<code> with an argument after the colon.
