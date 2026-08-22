"""Blind test: the state-file idioms. A missing, locked or half-written file
reads as the default (a fresh one each call); a write lands whole or not at
all, leaves no .tmp behind, and a value that will not serialize leaves the
previous file untouched. Run:
    .venv\\Scripts\\python tests\\test_statefile.py
"""
import _bootstrap  # noqa: F401
import tempfile
from pathlib import Path

import statefile


def main():
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "sub" / "x.json"                     # directory does not exist yet

    assert statefile.load_json(p) == {}
    assert statefile.load_json(p, list) == []
    a, b = statefile.load_json(p, list), statefile.load_json(p, list)
    assert a is not b, "default must be a factory - callers would share a list"

    statefile.atomic_write(p, {"a": 1})            # creates sub/ on the way
    assert statefile.load_json(p) == {"a": 1}
    assert not list(p.parent.glob("*.tmp")), "the tmp must be consumed by the replace"

    try:
        statefile.atomic_write(p, {"bad": object()})
        assert False, "an unserializable value must raise"
    except TypeError:
        pass
    assert statefile.load_json(p) == {"a": 1}, "a failed write must leave the old file"
    assert not list(p.parent.glob("*.tmp"))

    p.write_text('{"a": 1, "b"', encoding="utf-8")     # half a write, as a crash leaves
    assert statefile.load_json(p) == {}
    assert statefile.load_json(p, list) == []
    print("OK - statefile: default-factory reads, whole-or-nothing writes, no tmp left")


if __name__ == "__main__":
    main()
