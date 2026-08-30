"""Blind test: authenticated text API and shared conversational session."""
import json
import urllib.error
import urllib.request

import _bootstrap

import assistant_repl
import cglib
import text_interface


class FakeBackend:
    def __init__(self, secrets, model, effort=None, voice=None):
        self.model = model
        self.turns = 0

    def turn(self, system_text, user_text, impls):
        assert "general text assistant" in system_text
        self.turns += 1
        if "running" in user_text:
            result = impls["list_operations"]({"scope": "active"})
            assert result["operations"][0]["title"] == "Andor"
        return f"reply {self.turns}: {user_text}"


class FakeOperations:
    def for_assistant(self, scope, acknowledge=False):
        return [{"id": "op-andor", "title": "Andor", "state": "RUNNING",
                 "progress": {"phase": "waiting_for_match"}}]


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
    with urllib.request.urlopen(req, timeout=2) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main():
    cfg = json.loads(json.dumps(_bootstrap.CONFIG))
    cfg["textInterface"] = {"enabled": True, "host": "127.0.0.1", "port": 0}
    token = "t" * 64
    secrets = {"textInterfaceToken": token,
               "anthropicApiKey": "a" * 64}
    log = cglib.CapturingLog("voice")
    original = assistant_repl.BACKENDS["anthropic"]
    assistant_repl.BACKENDS["anthropic"] = FakeBackend
    server = text_interface.start(
        cfg, secrets, log, operations=FakeOperations())
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
        first = request(base + "/v1/chat", token, {
            "session": "couch", "message": "what is running?"})[1]
        second = request(base + "/v1/chat", token, {
            "session": "couch", "message": "and recently?"})[1]
        assert first["reply"] == "reply 1: what is running?"
        assert second["reply"] == "reply 2: and recently?"
        assert first["turn"] != second["turn"]
        assert len(log.find("text_request")) == 2
        calls = log.find("tool_call")
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_operations" and calls[0]["ok"]
    finally:
        server.shutdown()
        server.server_close()
        assistant_repl.BACKENDS["anthropic"] = original
    print("OK - text interface: bearer auth, health, and session continuity")


if __name__ == "__main__":
    main()
