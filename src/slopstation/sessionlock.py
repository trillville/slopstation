"""Coordinate ownership of the controller with a filesystem lock.

The lock contains a turn ID and process ID. Its modification time indicates
liveness, and acquisition is atomic so concurrent launches have one winner.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from slopstation import paths, statefile

LOCK_STALE_S = 300  # a live session touches the lock every few seconds


def lock_file():
    return paths.state("session.lock")


def last_error_file():
    """Written by couch.py when a launch fails; the listener buzzes it."""
    return paths.state("last_error")


def cancel_file():
    """The cancelling turn, written by the voice lane's end_session and
    consumed by every wait in couch.start()."""
    return paths.state("cancel")


def age() -> float | None:
    """Seconds since the lock was last touched, or None if there is no lock."""
    try:
        return time.time() - lock_file().stat().st_mtime
    except OSError:
        return None


def active(age_s: float | None = None) -> bool:
    """Return whether a recent session lock exists."""
    if age_s is None:
        age_s = age()
    return age_s is not None and age_s < LOCK_STALE_S


def acquire(content: str = "") -> bool:
    """Acquire an absent or stale session under the shared Windows byte lock."""
    with statefile.guard(lock_file()):
        if active():
            return False
        lock_file().write_text(content, encoding="utf-8")
        return True


def _owned() -> bool:
    """Called under guard; notes without a PID predate ownership tracking."""
    try:
        parts = lock_file().read_text(encoding="utf-8").split()
    except OSError:
        return False
    return len(parts) < 2 or parts[1] == str(os.getpid())


def touch() -> bool:
    """Freshen only our lock; False means the caller must stand down."""
    with statefile.guard(lock_file()):
        if not _owned():
            return False
        os.utime(lock_file())
        return True


def adopt(content: str, previous: str) -> bool:
    """Take over an existing lock when resuming a session after boot."""
    with statefile.guard(lock_file()):
        if lock_file().read_text(encoding="utf-8") != previous:
            return False
        lock_file().write_text(content, encoding="utf-8")
        return True


def release(before: Callable[[], None] | None = None) -> bool:
    """Finish our teardown before letting another process acquire the session."""
    with statefile.guard(lock_file()):
        if not _owned():
            return False
        if before is not None:
            before()
        lock_file().unlink(missing_ok=True)
        return True
