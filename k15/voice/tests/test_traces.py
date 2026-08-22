"""Blind test: traces write one JSON file per conversation, tolerate SDK
objects (model_dump), prune expired files on save, and fail soft. Run:
    .venv\\Scripts\\python tests\\test_traces.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import _bootstrap                               # noqa: F401,E402

import traces


class Block:                                    # stands in for an SDK content block
    def model_dump(self):
        return {"type": "text", "text": "hi"}


def main():
    tmp = Path(tempfile.mkdtemp())
    traces.DIR = tmp

    # Empty conversation -> no file.
    traces.save("voice", [])
    assert not list(tmp.glob("*.json"))

    # SDK objects serialize via model_dump; meta fields land in the doc.
    msgs = [{"role": "user", "content": "play hades"},
            {"role": "assistant", "content": [Block()]}]
    traces.save("voice", msgs, {"provider": "anthropic", "dry_run": True})
    files = list(tmp.glob("*-voice.json"))
    assert len(files) == 1, files
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["kind"] == "voice" and doc["provider"] == "anthropic"
    assert doc["messages"][1]["content"][0] == {"type": "text", "text": "hi"}
    print(f"  save: {files[0].name}, SDK block dumped to dict")

    # Expired file pruned by the NEXT save; the fresh one survives.
    old = tmp / "20200101-000000-voice.json"
    old.write_text("{}", encoding="utf-8")
    stale = time.time() - (traces.TTL_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    traces.save("repl-openai", [{"role": "user", "content": "hi"}])
    assert not old.exists() and files[0].exists()
    print(f"  prune: {old.name} gone after {traces.TTL_DAYS}d TTL, fresh kept")

    # Fail-soft: unwritable DIR (parent is a file) must not raise.
    blocker = tmp / "blocker"
    blocker.write_text("", encoding="utf-8")
    traces.DIR = blocker / "sub"
    traces.save("voice", msgs)
    print("  fail-soft: unwritable DIR logged, not raised")

    print("OK - traces: per-convo file, SDK dump, TTL prune, fail-soft")


if __name__ == "__main__":
    main()
