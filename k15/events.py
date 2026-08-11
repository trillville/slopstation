"""Structured events: one JSON object per line, beside the human log.

Every `log(...)` call in the couch system lands here twice - once as the
human line in couch.log (unchanged, still the documented first move when
something breaks) and once as a machine-readable record in
logs/{service}-YYYYMMDD.jsonl, which is what Grafana Alloy tails.

Design constraints, in the order they mattered:

* **Stdlib only.** The chord lane is load-bearing and runs on system python.
  Nothing here may add a dependency, open a socket, or block.
* **No dependency on cglib.** cglib imports *this*, and this is the emit
  boundary - the last place that should be able to fail on an import cycle.
  That is why secrets are loaded again here in six lines instead of reused.
* **Fail-soft, always.** A full disk, an unserializable object, or a locked
  file costs the event, never the caller. Same rule traces.py already lives by.
* **Daily files, never renamed.** Alloy holds a read handle open on Windows;
  a rename under it is a fight worth not having. The date is in the name and
  expired files are deleted, so no open handle is ever moved.

Also a tiny CLI, so the cmd.exe supervisors can emit too:

    python events.py emit supervisor restart code=1 what=listener
"""
import contextvars
import json
import os
import pathlib
import platform
import re
import sys
import time
import uuid

from datetime import datetime, timezone

BASE = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
TTL_DAYS = 14

DEBUG, INFO, WARN, ERROR = "debug", "info", "warn", "error"

# Reserved keys, emitted in this order so a raw line reads left-to-right like
# a sentence. Everything a caller passes is a free field appended after these.
_RESERVED = ("ts", "level", "env", "service", "lane", "event",
             "turn", "session", "job", "dur_ms", "err")

# Of those, the ones the EMITTER owns - a caller field of the same name would
# either shadow a Loki label or make the record lie about itself, so it gets
# renamed rather than dropped (losing the value silently would be worse).
# dur_ms/err are absent on purpose: those are ordinary caller fields.
_EMITTER_OWNED = frozenset(("ts", "level", "env", "service", "lane", "event",
                            "host"))

# Field names whose VALUE is always redacted, whatever it is. Belt to the
# secrets.json braces below: a key that never reaches secrets.json (an OAuth
# token in flight, say) is still caught by its name.
_SECRET_NAME_HINTS = ("key", "token", "secret", "password", "passwd", "pin",
                      "authorization", "auth")

# Human-line values longer than this are elided. The JSONL keeps them whole -
# this is only so a transcript does not wrap forty times in a console.
_HUMAN_MAX = 80


def _service():
    """The role this box plays, and a Loki label - so it stays low-cardinality
    and boring. Overridable for the bench; the hostname is a field, not this."""
    return os.environ.get("CG_SERVICE", "k15")


def _env():
    """prod unless we are demonstrably inside the blind suite.

    Auto-detected rather than opt-in because the failure mode we are fixing is
    exactly a test that forgot to say it was a test: couch.log has carried
    `trace save failed` lines from test_traces.py's fail-soft case for weeks,
    in a shape indistinguishable from a real outage."""
    override = os.environ.get("CG_ENV")
    if override:
        return override
    try:
        if "tests" in pathlib.Path(sys.argv[0]).resolve().parts:
            return "test"
    except (OSError, ValueError, IndexError):
        pass
    return "prod"


SERVICE = _service()
ENV = _env()
HOST = platform.node()

# Correlation, set once per user intent and inherited by everything downstream
# (E1 threads this from wake through to the gaming PC). A ContextVar rather
# than a global so the voice agent's concurrent sessions cannot bleed into
# each other; explicit kwargs always win over the ambient value.
_ctx = contextvars.ContextVar("cg_event_ctx", default={})


# One intent = one id, minted at the chord or the wake word and carried to
# the far side of the SSH boundary. Hex-only and length-capped BY DESIGN:
# Dispatch.ps1 writes it to a filename, so anything else is a path-traversal
# primitive. The same shape is enforced again in the Dispatch regex - this
# copy is convenience, that copy is the security boundary.
TURN_RE = re.compile(r"\A[0-9a-f]{1,8}\Z")


def new_turn():
    return uuid.uuid4().hex[:6]


def valid_turn(value):
    return bool(isinstance(value, str) and TURN_RE.match(value))


def context(**fields):
    """Set ambient correlation fields (turn=, session=, job=). Returns the
    token so a caller can restore; most callers just set and forget."""
    merged = dict(_ctx.get(), **{k: v for k, v in fields.items() if v is not None})
    return _ctx.set(merged)


def reset(token):
    try:
        _ctx.reset(token)
    except ValueError:
        pass                        # set in another context; nothing to undo


def current():
    return dict(_ctx.get())


# --- redaction ----------------------------------------------------------------

_redactions = None


def _secret_values():
    """Every real secret value, loaded once, so no key can ride out in a field.

    Deliberately re-implements cglib.load_secrets rather than importing it:
    cglib imports this module, and the emit boundary must not be the thing
    that breaks on a cycle."""
    global _redactions
    if _redactions is None:
        vals = set()
        try:
            raw = json.loads((BASE / "secrets.json").read_text(encoding="utf-8-sig"))
            for k, v in raw.items():
                if k.startswith("_"):
                    continue
                # Same placeholder rule as cglib.real_key: template junk is
                # not a secret, and redacting "..." would black out prose.
                if (isinstance(v, str) and "..." not in v
                        and not v.upper().startswith("PLACEHOLDER")
                        and len(v.strip()) >= 15):
                    vals.add(v.strip())
        except (OSError, ValueError, AttributeError):
            pass                    # no secrets file on this box: nothing to hide
        _redactions = vals
    return _redactions


def scrub(key, value):
    """Redact by field name, then by value. Returns the safe value."""
    if any(h in key.lower() for h in _SECRET_NAME_HINTS):
        return "***"
    if isinstance(value, str) and value:
        for secret in _secret_values():
            if secret in value:
                value = value.replace(secret, "***")
    return value


# --- the write path -----------------------------------------------------------

_last_day = None


def _path(day):
    stem = "test" if ENV == "test" else SERVICE
    return LOG_DIR / f"{stem}-{day}.jsonl"


def _prune():
    """Delete expired daily files. Called only when the date rolls over, not
    per line - a glob per event would be a real cost for no benefit."""
    cutoff = time.time() - TTL_DAYS * 86400
    try:
        for f in LOG_DIR.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass                # locked or vanished: next rollover
    except OSError:
        pass


def emit(lane, event, level=INFO, /, **fields):
    """Append one event. Never raises, never blocks on anything but a local
    append. Returns the record (handy in tests); None if it could not be
    built at all.

    EVERY parameter is POSITIONAL-ONLY (the `/`), so no caller field name can
    ever collide with one - they all land in **fields. Without it,
    `log("lane_up", lane="assistant")` raises TypeError at call BINDING,
    before any try/except in here can run, and takes down the caller. Which
    is exactly what it did: a crash-looping voice agent, caused by the
    telemetry that was supposed to explain crashes. Argument binding is the
    one failure this module cannot catch from the inside, so it is designed
    out rather than guarded against."""
    global _last_day
    try:
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y%m%d")
        ctx = _ctx.get()
        rec = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
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
            # A caller field must never overwrite a label or make the record
            # misdescribe itself; keep the value under a prefixed name so
            # nothing is lost quietly.
            rec["f_" + k if k in _EMITTER_OWNED else k] = scrub(k, v)
    except Exception:
        return None

    try:
        if day != _last_day:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _prune()
            _last_day = day
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with _path(day).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, ValueError, TypeError):
        pass                        # the event is lost; the caller is not
    return rec


# --- the human line -----------------------------------------------------------

def human(event, level=INFO, /, **fields):
    """Render an event the way couch.log has always read: the event, then its
    fields as k=v. Values are elided at 80 chars so a transcript does not
    swamp a console - the JSONL keeps them whole."""
    parts = [event]
    for k, v in fields.items():
        if v is None or k in ("turn", "session", "job"):
            continue              # correlation ids are for the machine
        v = scrub(k, v)
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        if len(s) > _HUMAN_MAX:
            # ASCII on purpose: this line is printed to a cmd.exe console,
            # where a cp1252 codepage turns a real ellipsis into a mojibake
            # box at best and a UnicodeEncodeError at worst.
            s = s[:_HUMAN_MAX - 3] + "..."
        if isinstance(v, str) and (" " in s or not s):
            s = f'"{s}"'
        parts.append(f"{k}={s}")
    prefix = "" if level == INFO else level.upper() + " "
    return prefix + " ".join(parts)


# --- CLI (for the cmd.exe supervisors) ----------------------------------------

def _cli(argv):
    """events.py emit <lane> <event> [--level warn] [k=v ...]

    Exists so Start-Listener.bat and friends can report a crash-restart. That
    line is currently a bare `echo >> couch.log` that nothing watches, which
    makes a chord-lane crash loop - the load-bearing lane - invisible."""
    if not argv or argv[0] != "emit" or len(argv) < 3:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    lane, event, level, fields = argv[1], argv[2], INFO, {}
    rest = argv[3:]
    while rest:
        a = rest.pop(0)
        if a == "--level" and rest:
            level = rest.pop(0)
        elif "=" in a:
            k, _, v = a.partition("=")
            # cmd.exe hands everything over as text; a numeric-looking value
            # is far more useful to a dashboard as a number (code=1 should be
            # comparable, not just groupable).
            try:
                fields[k] = int(v)
            except ValueError:
                fields[k] = v
        # anything else is ignored: a supervisor must never die on its own
        # telemetry, and cmd.exe quoting is a hostile environment.
    rec = emit(lane, event, level, **fields)        # positional: see emit()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{lane}] "
          + human(event, level, **fields), flush=True)
    return 0 if rec else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
