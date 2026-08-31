"""Conversation traces: one JSON file per conversation under state/traces/.

The voice pipeline dumps its context at session end, the REPL on exit.
Best-effort - a trace must never cost a session. No scheduler: each write
prunes anything older than TTL_DAYS.
"""
import json
import time

import cglib

DIR = cglib.STATE / "traces"
TTL_DAYS = 14

log = cglib.make_log("traces")


def _jsonable(o):
    dump = getattr(o, "model_dump", None)       # SDK/pydantic content blocks
    return dump() if callable(dump) else str(o)


def save(kind, messages, meta=None):
    """Write {stamp}-{kind}.json and prune expired traces. Fail-soft: a full
    disk or an unserializable object costs the trace, not the caller."""
    if not messages:
        return
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        path = DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.json"
        doc = dict(meta or {}, kind=kind,
                   t=time.strftime("%Y-%m-%d %H:%M:%S"), messages=messages)
        path.write_text(json.dumps(doc, indent=1, default=_jsonable),
                        encoding="utf-8")
        cutoff = time.time() - TTL_DAYS * 86400
        expired = [f for f in DIR.glob("*.json") if f.stat().st_mtime < cutoff]
        for f in expired:
            try:
                f.unlink()
            except OSError:
                pass                            # locked/open file: next save
        log("trace_saved", file=path.name, messages=len(messages),
            pruned=len(expired) or None)
    except Exception as e:
        log.warn("trace_save_failed", err=str(e))
