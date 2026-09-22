"""Shared test fixtures and helpers."""

import dataclasses
import functools
import json
import os
import time
import types
from pathlib import Path
from typing import Any

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


def toolkit_impls(dispatch, log, **services):
    """Every callable tool for the given services, keyed by name."""
    from slopstation.agent.llm.assistant import Toolkit

    return Toolkit(dispatch, log, **services).impls


def fake_dispatch(turn="aa0001", asked="", dry_run=False):
    """The two attributes of Dispatch a tool reads: dry_run and the utterance.
    turn=None is no utterance, which the confirmation gate refuses."""
    utterance = None if turn is None else types.SimpleNamespace(turn=turn, asked=asked)
    return types.SimpleNamespace(dry_run=dry_run, utterance=utterance)


def sonarr_episode(
    id, season, *, number=None, has_file=None, monitored=None, aired=None, file_id=None
):
    """One Sonarr episode row with only the keys given. The code reads the
    absence of airDateUtc and monitored, so the helper must not fill them."""
    row = {"id": id, "seasonNumber": season}
    if number is not None:
        row["episodeNumber"] = number
    if has_file is not None:
        row["hasFile"] = has_file
    if monitored is not None:
        row["monitored"] = monitored
    if aired is not None:
        row["airDateUtc"] = aired
    if file_id is not None:
        row["episodeFileId"] = file_id
    return row


@dataclasses.dataclass
class FakeArr:
    """One Arr app: answers GETs from the rows it holds and records every
    write. The rows are turned by name, so a typo is a failure rather than a
    new attribute."""

    name: str
    profiles: list = dataclasses.field(default_factory=list)
    lookup: list = dataclasses.field(default_factory=list)
    lookup_by_id: Any = None
    library: list = dataclasses.field(default_factory=list)
    episodes: list = dataclasses.field(default_factory=list)
    movie_files: list = dataclasses.field(default_factory=list)
    queue: dict = dataclasses.field(default_factory=lambda: {"records": []})
    history: dict = dataclasses.field(default_factory=lambda: {"records": []})
    health: list = dataclasses.field(default_factory=list)
    indexers: list = dataclasses.field(
        default_factory=lambda: [
            {"id": 1, "enable": True, "enableAutomaticSearch": True}
        ]
    )
    posts: list = dataclasses.field(default_factory=list)
    puts: list = dataclasses.field(default_factory=list)
    deletes: list = dataclasses.field(default_factory=list)
    commands: dict = dataclasses.field(default_factory=dict)
    created: Any = None

    def set(self, **rows):
        for name, value in rows.items():
            getattr(self, name)
            setattr(self, name, value)
        return self

    def get(self, endpoint, params=None):
        if endpoint == "qualityprofile":
            return list(self.profiles)
        if endpoint in ("movie/lookup", "series/lookup"):
            return list(self.lookup)
        if endpoint == "movie/lookup/tmdb":
            return dict(self.lookup_by_id) if self.lookup_by_id else {}
        if endpoint in ("movie", "series"):
            return [dict(row) for row in self.library]
        if endpoint.startswith(("movie/", "series/")):
            wanted = int(endpoint.split("/")[1])
            return next(dict(row) for row in self.library if row["id"] == wanted)
        if endpoint == "episode":
            return [dict(row) for row in self.episodes]
        if endpoint == "moviefile":
            return [dict(row) for row in self.movie_files]
        if endpoint == "queue":
            return self.queue
        if endpoint == "history":
            return self.history
        if endpoint.startswith("command/"):
            command_id = int(endpoint.split("/")[1])
            return dict(
                self.commands.get(
                    command_id,
                    {"id": command_id, "status": "completed", "result": "successful"},
                )
            )
        if endpoint == "health":
            return [dict(row) for row in self.health]
        if endpoint == "indexer":
            return [dict(row) for row in self.indexers]
        raise AssertionError((self.name, "GET", endpoint, params))

    def post(self, endpoint, payload):
        self.posts.append((endpoint, json.loads(json.dumps(payload))))
        if endpoint in ("movie", "series"):
            return dict(self.created)
        if endpoint == "command":
            command_id = len(self.commands) + 1
            self.commands[command_id] = {
                "id": command_id,
                "status": "started",
                "result": "unknown",
            }
            return dict(self.commands[command_id])
        raise AssertionError((self.name, "POST", endpoint, payload))

    def put(self, endpoint, payload):
        self.puts.append((endpoint, json.loads(json.dumps(payload))))
        if endpoint == "episode/monitor":
            episode_ids = set(payload["episodeIds"])
            for episode in self.episodes:
                if episode.get("id") in episode_ids:
                    episode["monitored"] = bool(payload["monitored"])
        elif endpoint.startswith(("movie/", "series/")):
            wanted = int(endpoint.split("/")[1])
            for index, row in enumerate(self.library):
                if int(row["id"]) == wanted:
                    self.library[index] = json.loads(json.dumps(payload))
        return dict(payload)

    def delete(self, endpoint, params=None):
        self.deletes.append((endpoint, params))
