"""Coordinate ownership of the controller with a filesystem lock.

The lock contains a turn ID and process ID. Its modification time indicates
liveness, and acquisition is atomic so concurrent launches have one winner.
"""

from __future__ import annotations

import os
import time

from slopstation import paths

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


def _recycle_stale(content: str) -> bool:
    """Atomically replace a stale lock and return whether this call won."""
    guard = lock_file().with_name("session.lock.recycle")
    try:
        if time.time() - guard.stat().st_mtime > 10:
            guard.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        f = open(guard, "x", encoding="utf-8")
    except OSError:
        return False  # someone else is recycling right now
    swapped = False
    try:
        with f:
            if active():
                return False  # a racer took it while we opened the guard
            f.write(content)
        # Windows cannot replace an open file. Recheck ownership after a
        # collision so a live lock is never replaced.
        for _ in range(8):
            try:
                os.replace(guard, lock_file())
                swapped = True
                return True
            except OSError:
                if active():
                    return False
        return False
    except OSError:
        return False  # the guard write failed; nothing was touched
    finally:
        if not swapped:
            try:
                guard.unlink(missing_ok=True)
            except OSError:
                pass


def acquire(content: str = "") -> bool:
    """Acquire the session lock and store its owner note."""
    lock_file().parent.mkdir(exist_ok=True)
    denied = None
    for attempt in (1, 2, 3):
        try:
            with open(lock_file(), "x", encoding="utf-8") as f:
                f.write(content)
            return True
        except FileExistsError:
            denied = None
        except PermissionError as e:
            # Windows may report a racing create as a sharing violation.
            denied = e
        if active() or attempt == 3:
            break
        if _recycle_stale(content):
            return True
    if denied is not None and not lock_file().exists():
        raise denied
    return False


def touch() -> None:
    """Freshen the mtime without rewriting the owner note."""
    try:
        os.utime(lock_file())
    except OSError:
        pass


def adopt(content: str) -> None:
    """Replace an existing lock's owner note and refresh its timestamp."""
    try:
        lock_file().write_text(content, encoding="utf-8")
    except OSError:
        pass


def release() -> bool:
    """Remove the lock if this process still owns it."""
    try:
        parts = lock_file().read_text(encoding="utf-8").split()
    except OSError:
        return False  # already gone: nothing to release
    if len(parts) >= 2 and parts[1] != str(os.getpid()):
        return False
    lock_file().unlink(missing_ok=True)
    return True
