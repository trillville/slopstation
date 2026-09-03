"""The session lock and its marker files, shared by couch.py, the chord
listener and the voice agent's dispatch.

One file, `state/session.lock`, says whether a launch or a live session owns
the Puck. Its mtime is the only datum liveness keys on; its content is the
owner note ("<turn> <pid>") release() checks. Taking it is one filesystem
operation - an exclusive create, or an atomic swap over a stale lock - so
racing launches produce exactly one winner.
"""

from __future__ import annotations

import os
import time

from slopstation import paths


def lock_file():
    return paths.state("session.lock")


LOCK_STALE_S = 300  # a live session touches the lock every few seconds


# written by couch.py on launch failure
def last_error_file():
    return paths.state("last_error")


# One line, the cancelling turn (may be empty). Written by voice end_session,
# unlinked by couch.py at every launch wait; stale copies voided at the next
# launch's start.
def cancel_file():
    return paths.state("cancel")


def age() -> float | None:
    """Seconds since the session lock was last touched, or None if no lock."""
    try:
        return time.time() - lock_file().stat().st_mtime
    except OSError:
        return None


def active(age_s: float | None = None) -> bool:
    """True while a launch or a live session owns the Puck; couch.py holds the
    lock fresh from before its first side effect through teardown. Pass the
    age to take the decision and the log field from one stat. A STALE lock
    deliberately reads as free: worst case LOCK_STALE_S of deafness, versus a
    permanently deaf chord lane."""
    if age_s is None:
        age_s = age()
    return age_s is not None and age_s < LOCK_STALE_S


def _recycle_stale(content: str) -> bool:
    """Take over a stale lock, one racer at a time; True if THIS call now owns
    it. The takeover must be ONE os.replace, never unlink-then-create: an
    empty path lets a racer's exclusive create land, and two callers win.

    The guard's exclusive create (O_EXCL) serializes recyclers, the staleness
    re-check happens INSIDE it, and the guard doubles as the incoming lock the
    swap consumes. os.replace never empties the path, so no create slips inside
    the swap, and a recycler arriving after it reads a fresh lock_file() and stands
    down. A guard orphaned mid-section is recycled at 10 s."""
    guard = lock_file().with_name(lock_file().name + ".recycle")
    try:
        if time.time() - guard.stat().st_mtime > 10:
            guard.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False  # someone else is recycling right now
    try:
        if active():
            return False  # a racer took it while we opened the guard
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None  # type: ignore[assignment] # Windows will not rename an open file
        # Windows needs the rename destination unopened, and a losing racer's
        # active() stat denies it - ~27% of swaps against a stat spin, so
        # retry. A denied swap changes nothing; only staleness must be
        # re-read, or a release landing in between puts a live lock under it.
        for _ in range(8):
            try:
                os.replace(guard, lock_file())
                guard = None  # type: ignore[assignment] # consumed by the swap; not ours to unlink
                return True
            except OSError:
                if active():
                    return False  # now someone's live lock; leave it alone
        return False
    except OSError:
        return False  # guard write failed; nothing was touched
    finally:
        if fd is not None:
            os.close(fd)
        if guard is not None:
            try:
                guard.unlink(missing_ok=True)
            except OSError:
                pass


def acquire(content: str = "") -> bool:
    """Take the session lock, or answer no. True only if THIS call put the
    file there - by exclusive create, or by the atomic swap over a stale lock.
    Each is a single filesystem operation, so racing launches produce exactly
    one winner; check-then-write does not, and two Enters recycle the Puck
    claim under the live session (controller goes input-dead). `content` is
    the owner note (couch.py writes "<turn> <pid>") read back by release();
    mtime stays the only datum active() keys on."""
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
            # Windows spells a RACING create as a sharing violation, not
            # FileExistsError. A real ACL problem lands here too, told apart
            # below by no lock existing once the dust settles.
            denied = e
        if active() or attempt == 3:
            break
        if _recycle_stale(content):
            return True
    if denied is not None and not lock_file().exists():
        raise denied
    return False


def touch() -> None:
    """Freshen mtime WITHOUT rewriting content: the owner note has to survive
    the session for release()'s ownership check."""
    try:
        os.utime(lock_file())
    except OSError:
        pass


def adopt(content: str) -> None:
    """Take over an existing lock (reconcile's resume): rewrite the owner note
    so release() recognizes us. Doubles as the first heartbeat."""
    try:
        lock_file().write_text(content, encoding="utf-8")
    except OSError:
        pass


def release() -> bool:
    """Unlink the session lock IF this process still owns it; True if it did.

    The owner note's pid is the check: a lock recycled out from under us is
    the successor's, and unlinking it would free a live session. A note with
    no readable pid releases anyway."""
    try:
        parts = lock_file().read_text(encoding="utf-8").split()
    except OSError:
        return False  # already gone: nothing to release
    if len(parts) >= 2 and parts[1] != str(os.getpid()):
        return False
    lock_file().unlink(missing_ok=True)
    return True
