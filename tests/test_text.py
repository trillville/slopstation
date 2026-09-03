"""Authenticated text API and shared conversational session."""

import json
import threading
import urllib.error
import urllib.request

import helpers
from slopstation import cglib
from slopstation.agent.brain import backends
from slopstation.agent.interfaces import text
from slopstation.agent.telemetry import traces

BLOCKED = threading.Event()  # a "stall" turn signals it is inside turn()
RELEASE = threading.Event()  # and waits here, wedging its session


class FakeBackend:
    def __init__(self, secrets, model, effort=None, voice=None):
        self.model = model
        self.turns = 0
        self.messages = []

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
            BLOCKED.set()
            assert RELEASE.wait(timeout=10)
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


def test_text():
    cfg = json.loads(json.dumps(helpers.CONFIG))
    cfg["textInterface"] = {"enabled": True, "host": "127.0.0.1", "port": 0}
    token = "t" * 64
    secrets = {"textInterfaceToken": token, "anthropicApiKey": "a" * 64}
    log = cglib.CapturingLog("voice")
    original = backends.BACKENDS["anthropic"]
    backends.BACKENDS["anthropic"] = FakeBackend
    saved = []
    original_save = traces.save
    traces.save = lambda kind, messages, meta=None, stem=None: saved.append(
        (kind, len(messages), (meta or {}).get("session"), stem)
    )
    dry = text.TextApplication(cfg, secrets, log, dry_run=True)
    assert dry._new_session()["dispatch"].dry_run
    server = text.start(
        cfg, secrets, log, operations=FakeOperations(), media=FakeMedia()
    )
    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"
        try:
            request(base + "/health")
            raise AssertionError("health endpoint accepted no token")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        status, health = request(base + "/health", token)
        assert status == 200 and health["ok"]
        first = request(
            base + "/v1/chat",
            token,
            {"session": "couch", "message": "what is running?"},
        )[1]
        second = request(
            base + "/v1/chat", token, {"session": "couch", "message": "and recently?"}
        )[1]
        assert first["reply"] == "reply 1: what is running?"
        assert second["reply"] == "reply 2: and recently?"
        acquired = request(
            base + "/v1/chat",
            token,
            {"session": "couch", "message": "download Andor season 1"},
        )[1]
        assert acquired["reply"] == (
            "Requested Andor, season 1, in 2160p. "
            "Sonarr is searching in the background."
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

        # A turn stuck inside the backend must not wedge its session: the
        # next request on it 503s as busy, while other sessions keep working.
        stalled = {}

        def stalled_turn():
            stalled["result"] = request(
                base + "/v1/chat",
                token,
                {"session": "wedged", "message": "please stall"},
            )

        stall_thread = threading.Thread(target=stalled_turn)
        stall_thread.start()
        assert BLOCKED.wait(timeout=5)
        try:
            request(
                base + "/v1/chat", token, {"session": "wedged", "message": "hello?"}
            )
            raise AssertionError("busy session accepted a second turn")
        except urllib.error.HTTPError as e:
            assert e.code == 503
            assert "previous message" in json.loads(e.read().decode("utf-8"))["error"]
        busy = log.find("text_session_busy")
        assert len(busy) == 1 and busy[0]["session"] == "wedged"
        status, other = request(
            base + "/v1/chat",
            token,
            {"session": "elsewhere", "message": "still with me?"},
        )
        assert status == 200 and other["ok"]
        RELEASE.set()
        stall_thread.join(timeout=10)
        assert stalled["result"][1]["ok"]

        # The SDK clients carry explicit deadlines: without them a stalled
        # provider stream outlives remote.py's 280 s forwarding budget.
        real = backends.AnthropicBackend({"anthropicApiKey": "a" * 64}, "claude-test")
        assert real.client.timeout == backends.LLM_TIMEOUT_S
        assert real.client.max_retries == backends.LLM_MAX_RETRIES
    finally:
        server.shutdown()
        server.server_close()
        traces.save = original_save
        backends.BACKENDS["anthropic"] = original
