"""Shared test fixtures and helpers."""

import functools
import json
import os
import time
from pathlib import Path

import pytest

from slopstation import events, logbook, sessionlock

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "slopstation"

# config.example.json as a dict: what config.current() answers under the suite.
CONFIG = json.loads((REPO / "config.example.json").read_text(encoding="utf-8-sig"))


def package_modules():
    """Every .py in the package."""
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def modname(path):
    """src/slopstation/agent/tools/library.py -> slopstation.agent.tools.library"""
    rel = path.relative_to(PACKAGE.parent).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


@functools.cache
def _present():
    """What this machine can run: steam (a local install - the gaming PC) and
    audio (real devices - the K15, opt-in because the mic is shared with a
    live lane). SLOPSTATION_TEST_HAS overrides the detection."""
    override = os.environ.get("SLOPSTATION_TEST_HAS")
    if override is not None:
        return frozenset(n for n in override.split(",") if n)
    found = set()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"):
            found.add("steam")
    except (ImportError, OSError):
        pass
    if os.environ.get("SLOPSTATION_TEST_AUDIO"):
        found.add("audio")
    return frozenset(found)


def wants(*needs):
    """Skip unless this machine has every need."""
    missing = [n for n in needs if n not in _present()]
    if missing:
        pytest.skip(f"needs {', '.join(missing)}")


def seed_lock(age_s, content="x"):
    """A session lock of that age in this test's runtime home; None removes it."""
    lock = sessionlock.lock_file()
    if age_s is None:
        lock.unlink(missing_ok=True)
        return
    lock.write_text(content)
    old = time.time() - age_s
    os.utime(lock, (old, old))


class CapturingLog(logbook.Logger):
    """The production logger's shape, recording instead of writing, so a change
    to the logging interface breaks the tests. Assert on events and fields."""

    def __init__(self, lane="test"):
        super().__init__(lane)
        self.records = []

    def _write(self, level, event, fields):
        rec = {
            ("f_" + k if k in events._EMITTER_OWNED else k): v
            for k, v in fields.items()
        }
        self.records.append(dict(rec, level=level, event=event))

    def events(self):
        return [r["event"] for r in self.records]

    def find(self, event):
        return [r for r in self.records if r["event"] == event]
