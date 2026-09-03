"""Authenticated text API and shared conversational session."""

import functools
import json
import threading
import urllib.error
import urllib.request

import pytest

import helpers
from slopstation import logbook
from slopstation.agent.brain import backends
from slopstation.agent.interfaces import text
from slopstation.agent.telemetry import traces

TOKEN = "t" * 64
SECRETS = {"textInterfaceToken": TOKEN, "anthropicApiKey": "a" * 64}


class Gate:
    """A "stall" turn signals it is inside turn() on `blocked`, then waits on
    `release`, wedging its session."""

    def __init__(self):
        self.blocked, self.release = threading.Event(), threading.Event()


class FakeBackend:
    def __init__(self, secrets, model, effort=None, voice=None, gate=None):
        self.gate: Gate = gate
        self.turns: int = 0
        self.messages: list[dict] = []

    def turn(self, system_text, user_text, impls):
        assert "general text assistant" in system_text
        self.turns += 1
        if "running" in user_text:
            result = impls["list_operations"]({"scope": "active"})
            assert result["operations"][0]["title"] == "Andor"
        if "download" in user_text:
            result = impls["request_series"](
                {"tvdb_id": 393189, "seasons": [1], "preset": "2160p"}
            )
            assert result["ok"]
        if "stall" in user_text:
            self.gate.blocked.set()
            assert self.gate.release.wait(timeout=10)
        self.messages.append({"role": "user", "content": user_text})
        return f"reply {self.turns}: {user_text}"


class FakeOperations:
    def track_external(self, *args, **kwargs):
        return {"id": "op-andor"}

    def observe(self, operation_id, state, progress, detail):
        return {"id": operation_id, "state": state, "progress": progress}

    def for_assistant(self, scope, acknowledge=False):
        return [
            {
                "id": "op-andor",
                "title": "Andor",
                "state": "RUNNING",
                "progress": {"phase": "waiting_for_match"},
            }
        ]


class FakeMedia:
    def request_series(self, tvdb_id, preset, seasons):
        return {
            "ok": True,
            "kind": "series_acquisition",
            "authority": "sonarr",
            "external_ref": "1",
            "title": "Andor",
            "catalog_id": tvdb_id,
            "preset": preset,
            "profile": "Slopstation Series 2160p",
            "seasons": seasons,
            "already_available": False,
        }


def request(url, token=None, payload=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Above BUSY_GRACE_S: a busy reply arrives only after the lock grace.
    with urllib.request.urlopen(req, timeout=8) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def chat(base, session, message):
    return request(base + "/v1/chat", TOKEN, {"session": session, "message": message})


@pytest.fixture
def cfg():
    cfg = json.loads(json.dumps(helpers.CONFIG))
    cfg["textInterface"] = {"enabled": True, "host": "127.0.0.1", "port": 0}
    return cfg


@pytest.fixture
def log():
    return logbook.CapturingLog("voice")


@pytest.fixture
def gate():
    return Gate()


@pytest.fixture
def fake_backend(monkeypatch, gate):
    """The provider the config names answers with FakeBackend, wired to this
    test's gate."""
    monkeypatch.setitem(
        backends.BACKENDS, "anthropic", functools.partial(FakeBackend, gate=gate)
    )


@pytest.fixture
def saved(monkeypatch):
    """What traces.save was handed: (kind, message count, session, stem)."""
    saved = []
    monkeypatch.setattr(
        traces,
        "save",
        lambda kind, messages, meta=None, stem=None: saved.append(
            (kind, len(messages), (meta or {}).get("session"), stem)
        ),
    )
    return saved


@pytest.fixture
def base(cfg, log, gate, fake_backend, saved):
    """A text interface on a free port, torn down after the test."""
    server = text.start(
        cfg, SECRETS, log, operations=FakeOperations(), media=FakeMedia()
    )
    assert server is not None
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        # A turn still wedged in the backend unblocks before the server goes.
        gate.release.set()
        server.shutdown()
        server.server_close()


def test_dry_run_reaches_the_dispatch(cfg, log, fake_backend):
    dry = text.TextApplication(cfg, SECRETS, log, dry_run=True)
    assert dry._new_session()["dispatch"].dry_run


def test_health_needs_the_token(base):
    with pytest.raises(urllib.error.HTTPError) as refused:
        request(base + "/health")
    assert refused.value.code == 401, "health endpoint accepted no token"
    status, health = request(base + "/health", TOKEN)
    assert status == 200 and health["ok"]


def test_a_session_carries_turns_tools_and_one_trace_file(base, log, saved):
    first = chat(base, "couch", "what is running?")[1]
    second = chat(base, "couch", "and recently?")[1]
    assert first["reply"] == "reply 1: what is running?"
    assert second["reply"] == "reply 2: and recently?"
    acquired = chat(base, "couch", "download Andor season 1")[1]
    assert acquired["reply"] == (
        "Requested Andor, season 1, in 2160p. Sonarr is searching in the background."
    )
    assert first["turn"] != second["turn"]
    assert len(log.find("text_request")) == 3
    calls = log.find("tool_call")
    assert len(calls) == 2
    assert calls[0]["tool"] == "list_operations" and calls[0]["ok"]
    assert calls[1]["tool"] == "request_series" and calls[1]["ok"]
    # Every turn of a session rewrites ONE trace file, so an MCP or LAN
    # request is as recoverable afterwards as a voice one.
    assert [k for k, _, _, _ in saved] == ["text"] * 3
    assert [n for _, n, _, _ in saved] == [1, 2, 3]
    assert {s for _, _, s, _ in saved} == {"couch"}
    assert len({stem for _, _, _, stem in saved}) == 1


def test_a_stalled_turn_wedges_only_its_own_session(base, log, gate):
    # A turn stuck inside the backend must not wedge its session: the
    # next request on it 503s as busy, while other sessions keep working.
    stalled = {}

    def stalled_turn():
        stalled["result"] = chat(base, "wedged", "please stall")

    stall_thread = threading.Thread(target=stalled_turn)
    stall_thread.start()
    assert gate.blocked.wait(timeout=5)
    with pytest.raises(urllib.error.HTTPError) as busy_reply:
        chat(base, "wedged", "hello?")
    assert busy_reply.value.code == 503, "busy session accepted a second turn"
    error = json.loads(busy_reply.value.read().decode("utf-8"))["error"]
    assert "previous message" in error
    busy = log.find("text_session_busy")
    assert len(busy) == 1 and busy[0]["session"] == "wedged"
    status, other = chat(base, "elsewhere", "still with me?")
    assert status == 200 and other["ok"]
    gate.release.set()
    stall_thread.join(timeout=10)
    assert stalled["result"][1]["ok"]


def test_the_sdk_clients_carry_deadlines():
    # The SDK clients carry explicit deadlines: without them a stalled
    # provider stream outlives remote.py's 280 s forwarding budget.
    real = backends.AnthropicBackend({"anthropicApiKey": "a" * 64}, "claude-test")
    assert real.client.timeout == backends.LLM_TIMEOUT_S
    assert real.client.max_retries == backends.LLM_MAX_RETRIES
