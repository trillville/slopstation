"""The session lock and its marker files, shared by couch.py, the chord
listener and the voice agent's dispatch.

`state/session.lock` says whether a launch or a live session owns the Puck.
Its mtime is what liveness keys on; its content is the owner note
("<turn> <pid>") that release() checks. Taking it is one filesystem operation
- an exclusive create, or an atomic swap over a stale lock - so racing
launches produce exactly one winner.
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
    """True while a launch or a live session owns the Puck. A stale lock reads
    as free on purpose: LOCK_STALE_S of deafness beats a permanently deaf
    chord lane."""
    if age_s is None:
        age_s = age()
    return age_s is not None and age_s < LOCK_STALE_S


def _recycle_stale(content: str) -> bool:
    """Swap a fresh lock over a stale one; True if THIS call now owns it.

    The swap is one os.replace, never unlink-then-create, which would let two
    racers win. A guard file taken with O_EXCL serialises recyclers, the
    staleness check is repeated inside it, and the guard itself is what gets
    swapped in. A guard orphaned mid-section is recycled after 10 s."""
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
        # After the close: Windows will not rename an open file. A losing
        # racer's stat can deny the swap, so retry - but re-read staleness
        # first, or a release landing in between puts a live lock under us.
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
    """Take the session lock; True only if THIS call put the file there, by
    exclusive create or by the swap over a stale lock. `content` is the owner
    note ("<turn> <pid>") that release() reads back."""
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
            # Windows spells a racing create as a sharing violation. A real
            # ACL problem lands here too, told apart below by no lock existing
            # once the dust settles.
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
    """Take over an existing lock (reconcile's resume): rewrite the owner note
    so release() recognises us. Doubles as the first heartbeat."""
    try:
        lock_file().write_text(content, encoding="utf-8")
    except OSError:
        pass


def release() -> bool:
    """Unlink the lock if this process still owns it; True if it did. A lock
    recycled out from under us is the successor's, and unlinking it would
    free a live session. A note with no readable pid releases anyway."""
    try:
        parts = lock_file().read_text(encoding="utf-8").split()
    except OSError:
        return False  # already gone: nothing to release
    if len(parts) >= 2 and parts[1] != str(os.getpid()):
        return False
    lock_file().unlink(missing_ok=True)
    return True
