"""Read, write, and lock JSON state files."""

from __future__ import annotations

import contextlib
import json
import msvcrt
import os
import pathlib
import time
from typing import Any


def load(path: pathlib.Path, default: Any) -> Any:
    """A JSON state file, or `default` when absent or unparseable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def guard(path: pathlib.Path):
    """Serialize a state file update across threads and processes.

    LK_NBLCK spins because LK_LOCK retries on its own 1 s timer. Windows drops
    the byte lock when the holder dies, so a killed CLI cannot wedge the agent."""
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.005)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def write(path: pathlib.Path, obj: Any, indent: int = 1) -> None:
    """tmp + os.replace, so a reader never sees a partial file. The replace
    retries: Windows denies a rename onto a file another process holds open
    (doctor reads operations.json). Callers serialize writers with guard()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 7:
                raise
            time.sleep(0.05)
