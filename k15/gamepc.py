"""The gaming PC as the K15 sees it: one ssh call per Dispatch.ps1 verb, the
turn tag on the mutating ones, and the answer words. Dispatch.ps1 is the
server; voice/tests/test_turn.py holds the two in step.
"""
import subprocess

import cglib
import events


def ssh(cmd, timeout=15):
    """Run one Dispatch verb on the host; returns its stdout.

    stdout only, so stderr noise stays out of state comparisons; Dispatch
    reports its own failures as FAILED:<code>. check=True is load-bearing: an
    unreachable host RAISES instead of returning ssh's error text as session
    state - otherwise the READY poll reads 'ssh: connect ... timed out' as
    READY, and watch() never detects sleep."""
    r = subprocess.run(["ssh", cglib.config()["sshHost"], cmd],
                       capture_output=True, text=True, timeout=timeout, check=True)
    return r.stdout.strip()


def ssh_intent(cmd, turn=None, **kw):
    """A MUTATING verb, tagged with this launch's turn id; read-only polls use
    plain ssh(). Pass `turn` explicitly from callers whose ambient context
    predates the utterance (the voice lane's - a ContextVar cannot reach it)."""
    turn = turn or events.current().get("turn")
    # Dispatch fails CLOSED on a malformed id (matches no verb, answers
    # DENIED), so re-validate here and send it uncorrelated instead.
    return ssh(f"{cmd} --turn {turn}" if events.valid_turn(turn) else cmd, **kw)


def enter_running():
    """True/False if the gaming PC could tell us whether its Enter task is
    still running; None if it could not. The None is load-bearing: a PC
    predating `enterstate` answers DENIED and an ssh blip raises, and
    re-dispatching on either would fight a healthy Enter."""
    try:
        ans = ssh("enterstate")
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

def enter(turn=None):
    return ssh_intent("enter", turn)


def exit(turn=None):
    return ssh_intent("exit", turn)


def status():
    return ssh("status")


def enterstate():
    return ssh("enterstate")


def version():
    return ssh("version")


def playing():
    return ssh("playing")


def games():
    return ssh("games", timeout=30)


def collections():
    return ssh("collections", timeout=15)


def launch(appid, turn=None):
    return ssh_intent(f"launch {int(appid)}", turn)


def stop(appid, turn=None):
    return ssh_intent(f"stop {int(appid)}", turn)


def nav(kind, arg=None, turn=None):
    return ssh_intent(nav_cmd(kind, arg), turn)


def nav_cmd(kind, arg=None):
    return f"nav {kind}" + (f" {arg}" if arg not in (None, "") else "")


# Answers: OK NOTREADY ALREADY NOTRUNNING NOTINSTALLED RUNNING IDLE DENIED, and
# BUSY:<appid> NOTASK:<name> FAILED:<code> with an argument after the colon.
def split_answer(out):
    word, _, arg = (out or "").partition(":")
    return word, arg
