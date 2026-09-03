"""The per-lane logger: every call prints, appends the human line to couch.log,
and emits the same event as JSON for the log shipper (events.py).

Event names are a closed vocabulary that dashboards group by and alerts fire
on, so variable data goes in fields, never in the name; tests/test_event_names
freezes the set. warn/error mean the user lost something they would notice.
"""

from __future__ import annotations

import os
import time
from typing import Any

from slopstation import events, paths


def rotate(max_bytes: int = 5_000_000) -> None:
    """Two-generation rotation: couch.log -> couch.log.1 past the cap. Called
    at K15 boot (reconcile) and listener startup. Writers open-append-close
    per line, so a lost rename just rotates on the next call."""
    logf = paths.couch_log()
    try:
        if logf.stat().st_size > max_bytes:
            os.replace(logf, logf.with_suffix(".log.1"))
    except OSError:
        pass


class Logger:
    """Called as `log("event", field=value, ...)`. Under the test suite
    (env=test) the console still gets everything but couch.log does not."""

    def __init__(self, lane: str) -> None:
        self.lane = lane

    def _write(self, level: str, event: str, fields: dict) -> None:
        # The whole body is guarded, not just the I/O: every log call funnels
        # through here, so anything that raises crashes the lane.
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

    # Three levels, not four; `info` is the spelled-out form of __call__. No
    # `debug`: `level` becomes the log record's SEVERITY, which alerts key on,
    # so an unemitted level is a permanently empty dashboard value.
    def info(self, event: str, /, **fields: Any) -> None:
        self._write(events.INFO, event, fields)

    def warn(self, event: str, /, **fields: Any) -> None:
        self._write(events.WARN, event, fields)

    def error(self, event: str, /, **fields: Any) -> None:
        self._write(events.ERROR, event, fields)


def logger(lane: str) -> Logger:
    """One logger per lane. The lane is a log attribute alerts select on, so
    the set stays small and fixed: tests/test_event_names.py LANES is the
    enforced list."""
    return Logger(lane)


class CapturingLog(Logger):
    """Test double with the PRODUCTION shape - same signature, same levels -
    recording instead of writing, so a change to the logging interface breaks
    the tests. Assert on events and fields, never prose."""

    def __init__(self, lane: str = "test", echo: bool = False) -> None:
        super().__init__(lane)
        self.records: list[dict] = []
        self.echo = echo

    def _write(self, level: str, event: str, fields: dict) -> None:
        rec = {
            ("f_" + k if k in events._EMITTER_OWNED else k): v
            for k, v in fields.items()
        }
        self.records.append(dict(rec, level=level, event=event))
        if self.echo:
            print(f"[{self.lane}] " + events.human(event, level, **fields))

    def events(self) -> list[str]:
        return [r["event"] for r in self.records]

    def find(self, event: str) -> list[dict]:
        return [r for r in self.records if r["event"] == event]
