"""Test conversation trace writing and retention."""

import json
import os
import time

import pytest

from slopstation.agent.telemetry import traces


class Block:  # stands in for an SDK content block
    def model_dump(self):
        return {"type": "text", "text": "hi"}


MSGS = [
    {"role": "user", "content": "play hades"},
    {"role": "assistant", "content": [Block()]},
]


@pytest.fixture
def trace_dir():
    """state/traces under this test's runtime home (conftest points paths.HOME
    at tmp_path), created up front so the globs below read a real directory."""
    d = traces.directory()
    d.mkdir(parents=True)
    return d


def test_empty_conversation_writes_no_file(trace_dir):
    traces.save("voice", [])
    assert not list(trace_dir.glob("*.json"))


def test_sdk_objects_serialize_and_meta_lands_in_the_doc(trace_dir):
    # SDK objects serialize via model_dump; meta fields land in the doc.
    traces.save("voice", MSGS, {"provider": "anthropic", "dry_run": True})
    files = list(trace_dir.glob("*-voice.json"))
    assert len(files) == 1, files
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["kind"] == "voice" and doc["provider"] == "anthropic"
    assert doc["messages"][1]["content"][0] == {"type": "text", "text": "hi"}


def test_expired_file_is_pruned_by_the_next_save(trace_dir):
    traces.save("voice", MSGS)
    (fresh,) = trace_dir.glob("*-voice.json")
    # Expired file pruned by the NEXT save; the fresh one survives.
    old = trace_dir / "20200101-000000-voice.json"
    old.write_text("{}", encoding="utf-8")
    stale = time.time() - (traces.TTL_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    traces.save("repl-openai", [{"role": "user", "content": "hi"}])
    assert not old.exists() and fresh.exists()


def test_unwritable_directory_fails_soft(tmp_path, monkeypatch):
    # An unwritable trace directory does not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(traces, "directory", lambda: blocker / "sub")
    traces.save("voice", MSGS)
