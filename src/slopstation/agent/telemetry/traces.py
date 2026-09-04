"""Store conversation traces and remove expired files."""

import json
import time

from slopstation import logbook, paths


def directory():
    return paths.state("traces")


TTL_DAYS = 14

log = logbook.logger("traces")


def _jsonable(o):
    dump = getattr(o, "model_dump", None)  # SDK/pydantic content blocks
    return dump() if callable(dump) else str(o)


def save(kind, messages, meta=None, stem=None):
    """Write a trace and prune expired files.

    ``stem`` keeps repeated saves for one conversation in the same file.
    """
    if not messages:
        return
    try:
        directory().mkdir(parents=True, exist_ok=True)
        path = directory() / f"{stem or time.strftime('%Y%m%d-%H%M%S')}-{kind}.json"
        doc = dict(
            meta or {},
            kind=kind,
            t=time.strftime("%Y-%m-%d %H:%M:%S"),
            messages=messages,
        )
        path.write_text(json.dumps(doc, indent=1, default=_jsonable), encoding="utf-8")
        cutoff = time.time() - TTL_DAYS * 86400
        expired = [f for f in directory().glob("*.json") if f.stat().st_mtime < cutoff]
        for f in expired:
            try:
                f.unlink()
            except OSError:
                pass  # locked/open file: next save
        log(
            "trace_saved",
            file=path.name,
            messages=len(messages),
            pruned=len(expired) or None,
        )
    except Exception as e:
        log.warn("trace_save_failed", err=str(e))
