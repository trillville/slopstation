"""Write human-readable and structured event logs."""

from __future__ import annotations

import os
import time
from typing import Any

from slopstation import events, paths


def rotate(max_bytes: int = 5_000_000) -> None:
    """Rotate couch.log to couch.log.1 when it exceeds the size limit."""
    logf = paths.couch_log()
    try:
        if logf.stat().st_size > max_bytes:
            os.replace(logf, logf.with_name(logf.name + ".1"))
    except OSError:
        pass


class Logger:
    """Called as `log("event", field=value, ...)`; `warn` and `error` are the
    other two levels (no `debug`: the level becomes the record's severity,
    which alerts key on, so an unemitted level is an empty dashboard value).
    Under the test suite the console still gets everything but couch.log
    does not."""

    def __init__(self, lane: str) -> None:
        self.lane = lane

    def _write(self, level: str, event: str, fields: dict) -> None:
        # The whole body is guarded: every log call funnels through here, so
        # anything that raises crashes the lane.
        try:
            # level POSITIONAL on both calls - by keyword it would collide
            # with a caller field named `level` (see events.emit).
            line = (
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{self.lane}] "
                + events.human(event, level, **fields)
            )
            try:
                print(line, flush=True)
            except (OSError, ValueError, AttributeError, UnicodeError):
                pass  # windowless task: stdout is None or a dead pipe
            if events.ENV != "test":
                try:
                    with paths.couch_log().open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass
            events.emit(self.lane, event, level, **fields)
        except Exception:
            pass

    def __call__(self, event: str, /, **fields: Any) -> None:
        self._write(events.INFO, event, fields)

    def warn(self, event: str, /, **fields: Any) -> None:
        self._write(events.WARN, event, fields)

    def error(self, event: str, /, **fields: Any) -> None:
        self._write(events.ERROR, event, fields)


def logger(lane: str) -> Logger:
    """One logger per lane. The lane is a log attribute alerts select on, so
    the set stays small: tests/test_event_names.py LANES is the enforced list."""
    return Logger(lane)
