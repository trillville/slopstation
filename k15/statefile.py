"""The JSON state files under state/: one read idiom, one write idiom.

state/ is the file-backed state a human can inspect and delete at 2am (README
§ Deliberately not doing - no database), and every JSON file in it is read and
written by more than one process. Two idioms, each in one place:

  load_json(path, default=dict)  the contents, or default() when the file is
      missing, locked, or half a write - every reader fail-softs the same way,
      so a new caller cannot invent a fifth spelling of "or {}" (there were
      four in library.py alone, plus jobs.py's).
  atomic_write(path, obj)        tmp + os.replace, so a crash mid-write leaves
      the PREVIOUS file rather than a truncated one. jobs.json was the
      exception: rewritten on every status transition with a plain write,
      and its reader's fail-soft turned a truncated file into "no jobs ever
      existed" - which the reconciler, the house rule for every piece of
      distributed state, could not see, because it reads the same file.

Stdlib only: library.py lives on this from system python, jobs.py and
traces.py from the voice venv.
"""
import json
import os

import cglib

STATE = cglib.BASE / "state"


def load_json(path, default=dict):
    """The parsed file, or default() - a FACTORY, so two callers can never
    share one mutable fallback."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default()


def atomic_write(path, obj, indent=1):
    """Write obj as JSON via a sibling .tmp and os.replace: a reader sees the
    old file or the new one, never a partial. Creates the directory. A value
    that will not serialize raises before anything on disk is touched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
