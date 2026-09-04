"""Write structured JSONL events alongside the human-readable log.

Daily files are deleted when they expire rather than renamed because the log
shipper keeps them open on Windows. Event writes do not raise to callers.

CLI:

    python -m slopstation.events emit supervisor restart code=1 what=listener
"""

from __future__ import annotations

import contextvars
import json
import msvcrt
import os
import pathlib
import platform
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, TypeGuard

from slopstation import paths

TTL_DAYS = 14
# Subdirectory NAME, not a path: the test suite monkeypatches paths.logs(), and a
# module-level paths.logs() / "archive" would freeze the real path at import time
# and let a test write into the live log directory.
ARCHIVE_NAME = "archive"
ARCHIVE_DAYS = 2  # out of the shipper's glob; see _prune

# The whole level vocabulary. It becomes the log record's SEVERITY, which
# alerts group on, so it stays small and every value has an emitter
# (logbook.Logger: no `debug`).
INFO, WARN, ERROR = "info", "warn", "error"

# Keys the EMITTER owns. A caller field of the same name would shadow a log
# attribute or make the record lie, so it is renamed rather than dropped.
_EMITTER_OWNED = frozenset(("ts", "level", "env", "service", "lane", "event", "host"))

# Field names whose VALUE is always redacted. Belt to the secrets.json braces
# below: a key that never reaches secrets.json is still caught by its name.
_SECRET_NAME_HINTS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "pin",
    "authorization",
    "auth",
)

# Human-line values longer than this are elided; the JSONL keeps them whole.
_HUMAN_MAX = 80


# Record attributes alerts select on, so they stay low-cardinality and are
# read once. The test suite sets SLOPSTATION_ENV before anything imports this,
# so a test can never write a record indistinguishable from an outage.
SERVICE = os.environ.get("SLOPSTATION_SERVICE", "k15")
ENV = os.environ.get("SLOPSTATION_ENV", "prod")
HOST = platform.node()

# Correlation, set once per user intent and inherited downstream. A ContextVar
# so the voice agent's concurrent sessions cannot bleed into each other;
# explicit kwargs win over the ambient value. The default is read-only and
# every writer builds a fresh dict.
_ctx: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "event_ctx", default=MappingProxyType({})
)


# One intent = one id, minted at the chord or the wake word and carried across
# the SSH boundary. Hex-only and length-capped BY DESIGN: Dispatch.ps1 writes
# it to a filename, so anything else is a path-traversal primitive. The
# Dispatch regex enforces the same shape and is the security boundary; this
# copy is convenience.
TURN_RE = re.compile(r"\A[0-9a-f]{1,8}\Z")


def new_turn() -> str:
    return uuid.uuid4().hex[:6]


def valid_turn(value: object) -> TypeGuard[str]:
    return bool(isinstance(value, str) and TURN_RE.match(value))


def context(**fields: Any) -> contextvars.Token:
    """Set ambient correlation fields (turn=, session=, job=); returns the
    token for reset()."""
    merged = dict(_ctx.get(), **{k: v for k, v in fields.items() if v is not None})
    return _ctx.set(merged)


def reset(token: contextvars.Token) -> None:
    try:
        _ctx.reset(token)
    except ValueError:
        pass  # set in another context; nothing to undo


def current() -> dict:
    return dict(_ctx.get())


# --- redaction ----------------------------------------------------------------

_redactions = None


def load_secrets(path: str | pathlib.Path) -> dict:
    """secrets.json as a dict; {} when absent. Raises ValueError when present
    but malformed (config.secrets() turns that into a console note). utf-8-sig
    eats Notepad's BOM."""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def real_key(value: object) -> TypeGuard[str]:
    """Template junk ('dg_...', 'PLACEHOLDER...') reads as absent. Redacting
    "..." would black out prose."""
    return (
        isinstance(value, str)
        and "..." not in value
        and not value.upper().startswith("PLACEHOLDER")
        and len(value.strip()) >= 15
    )


def _secret_values() -> set[str]:
    """Every real secret value, loaded once, so no key can ride out in a
    field."""
    global _redactions
    if _redactions is None:
        vals = set()
        try:
            for k, v in load_secrets(paths.HOME / "secrets.json").items():
                if not k.startswith("_") and real_key(v):
                    vals.add(v.strip())
        except (OSError, ValueError, AttributeError):
            pass  # no secrets file on this box: nothing to hide
        _redactions = vals
    return _redactions


# Redact credentials embedded in URL query strings, including runtime tokens.
_SECRET_QUERY = re.compile(
    r"((?:access_token|refresh_token|token|api_?key|key|password|passwd"
    r"|secret|auth|nonce|sessionid|steamloginsecure)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)


def scrub(key: str, value: Any) -> Any:
    """Remove secrets identified by field name, value, or URL syntax."""
    if any(h in key.lower() for h in _SECRET_NAME_HINTS):
        return "***"
    if isinstance(value, str) and value:
        for secret in _secret_values():
            if secret in value:
                value = value.replace(secret, "***")
        value = _SECRET_QUERY.sub(r"\1***", value)
    return value


# --- the write path -----------------------------------------------------------

_last_day = None


def _path(day: str) -> pathlib.Path:
    stem = "test" if ENV == "test" else SERVICE
    return paths.logs() / f"{stem}-{day}.jsonl"


def _prune() -> None:
    """Move closed daily files out of the shipper's glob (every tailed file
    costs it CPU whether or not it can still change), then delete the expired
    ones from both folders. Called on a process's first emit and at date
    rollover. Moving a file the shipper holds open is safe on Windows: the
    handle follows the rename."""
    now = time.time()
    archive = paths.logs() / ARCHIVE_NAME
    try:
        archive.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        # No rglob: archive/ is not re-scanned, so nothing moves twice.
        for f in paths.logs().glob("*.jsonl"):
            try:
                if f.stat().st_mtime < now - ARCHIVE_DAYS * 86400:
                    f.replace(archive / f.name)
            except OSError:
                pass  # locked or vanished: next rollover
        for f in list(paths.logs().glob("*.jsonl")) + list(archive.glob("*.jsonl")):
            try:
                if f.stat().st_mtime < now - TTL_DAYS * 86400:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


# Windows emulates O_APPEND as seek-to-end THEN write, so two processes can
# pick the same offset and one silently overwrites the other (~20% loss with
# 8 concurrent emitters). Every writer takes a one-byte lock on a sidecar
# file first, for at most this long: an unlocked write beats a blocked lane.
LOCK_WAIT_S = 0.2


def _append(path: pathlib.Path, line: str) -> None:
    data = (line + chr(10)).encode("utf-8")
    fd = os.open(str(path.parent / ".emit.lock"), os.O_CREAT | os.O_RDWR)
    held = False
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                held = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.002)
        with path.open("ab") as f:
            f.write(data)
    finally:
        if held:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def emit(lane: str, event: str, level: str = INFO, /, **fields: Any) -> dict | None:
    """Append one event. Never raises, never blocks on anything but a local
    append. Returns the record (handy in tests); None if it could not be built.

    EVERY parameter is POSITIONAL-ONLY (the `/`), so no caller field name can
    collide - they all land in **fields. Otherwise `log("lane_up",
    lane="assistant")` raises TypeError at call BINDING, before any try/except
    in here can run."""
    global _last_day
    try:
        now = datetime.now(UTC)
        # Filename uses the LOCAL date; every ts inside stays UTC. Mixed on
        # purpose: timestamps must be unambiguous, but a filename dated
        # tomorrow from 5pm onwards (UTC-7) confuses a human. Retention prunes
        # by mtime, so naming never affects correctness.
        day = time.strftime("%Y%m%d")
        ctx = _ctx.get()
        rec = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{now.microsecond // 1000:03d}Z",
            "level": level,
            "env": ENV,
            "service": SERVICE,
            "lane": lane,
            "event": event,
        }
        for k in ("turn", "session", "job"):
            if k in fields and fields[k] is not None:
                rec[k] = fields.pop(k)
            elif ctx.get(k) is not None:
                rec[k] = ctx[k]
        rec["host"] = HOST
        for k, v in fields.items():
            if v is None:
                continue
            # A caller field must never overwrite a label; keep the value
            # under a prefixed name so nothing is lost quietly.
            rec["f_" + k if k in _EMITTER_OWNED else k] = scrub(k, v)
    except Exception:
        return None

    try:
        if day != _last_day:
            paths.logs().mkdir(parents=True, exist_ok=True)
            _prune()
            _last_day = day
        _append(_path(day), json.dumps(rec, default=str, ensure_ascii=False))
    except (OSError, ValueError, TypeError):
        pass  # the event is lost; the caller is not
    return rec


# --- liveness -----------------------------------------------------------------

HEARTBEAT_S = 60


class Ticker(threading.Thread):
    """A daemon thread that calls `tick` every `interval_s` for as long as the
    process runs, swallowing whatever it raises. Set `stop` to end it: a lane
    never does; the tests do."""

    def __init__(
        self, name: str, interval_s: float, tick: Callable[[], object]
    ) -> None:
        super().__init__(daemon=True, name=name)
        self.interval_s = interval_s
        self._tick = tick
        self.stop = threading.Event()

    def run(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                pass
            if self.stop.wait(self.interval_s):
                return


def start_heartbeat(lane: str, interval_s: float = HEARTBEAT_S) -> Ticker:
    """Emit `heartbeat` every minute for as long as this process runs. JSONL
    only, not through the lane logger: ~1440 lines a day would swamp
    couch.log. This proves the SHIPPER is alive, not the lane - a dead lane
    emits no rows at all, and a threshold over an empty window proves
    nothing. checkin.py carries lane liveness; read together, heartbeats
    missing while check-ins arrive is a dead collector."""
    t = Ticker(
        f"heartbeat-{lane}",
        interval_s,
        lambda: emit(lane, "heartbeat", INFO, interval_s=interval_s),
    )
    t.start()
    return t


# --- the human line -----------------------------------------------------------


def human(event: str, level: str = INFO, /, **fields: Any) -> str:
    """Render an event as couch.log reads it: the event, then fields as k=v.
    Values are elided at _HUMAN_MAX; the JSONL keeps them whole."""
    parts = [event]
    for k, v in fields.items():
        if v is None or k in ("turn", "session", "job"):
            continue  # correlation ids are for the machine
        v = scrub(k, v)
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        if len(s) > _HUMAN_MAX:
            # ASCII on purpose: printed to a cmd.exe console, where a cp1252
            # codepage turns a real ellipsis into mojibake or a
            # UnicodeEncodeError.
            s = s[: _HUMAN_MAX - 3] + "..."
        if isinstance(v, str) and (" " in s or not s):
            s = f'"{s}"'
        parts.append(f"{k}={s}")
    prefix = "" if level == INFO else level.upper() + " "
    return prefix + " ".join(parts)


# --- CLI ----------------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    """events.py emit <lane> <event> [--level warn] [k=v ...], for smart-alert.bat."""
    if not argv or argv[0] != "emit" or len(argv) < 3:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    lane, event, level = argv[1], argv[2], INFO
    fields: dict[str, int | str] = {}
    rest = argv[3:]
    while rest:
        a = rest.pop(0)
        if a == "--level" and rest:
            level = rest.pop(0)
        elif "=" in a:
            k, _, v = a.partition("=")
            # cmd.exe hands everything over as text; a number is more useful
            # to a dashboard as a number. Anything else is ignored: a caller
            # must never die on its own telemetry.
            try:
                fields[k] = int(v)
            except ValueError:
                fields[k] = v
    rec = emit(lane, event, level, **fields)
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{lane}] "
        + human(event, level, **fields),
        flush=True,
    )
    return 0 if rec else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
