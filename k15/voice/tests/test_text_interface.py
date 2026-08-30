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
        if "download" in user_text:
            result = impls["request_series"]({
                "tvdb_id": 393189, "seasons": [1], "preset": "2160p"})
            assert result["ok"]
        return f"reply {self.turns}: {user_text}"


class FakeOperations:
    def track_external(self, *args, **kwargs):
        return {"id": "op-andor"}

    def observe(self, operation_id, state, progress, detail):
        return {"id": operation_id, "state": state, "progress": progress}

    def for_assistant(self, scope, acknowledge=False):
        return [{"id": "op-andor", "title": "Andor", "state": "RUNNING",
                 "progress": {"phase": "waiting_for_match"}}]


class FakeMedia:
    def request_series(self, tvdb_id, preset, seasons):
        return {"ok": True, "kind": "series_acquisition",
                "authority": "sonarr", "external_ref": "1", "title": "Andor",
                "catalog_id": tvdb_id, "preset": preset,
                "profile": "Slopstation Series 2160p", "seasons": seasons,
                "already_available": False}


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
    dry = text_interface.TextApplication(cfg, secrets, log, dry_run=True)
    assert dry._new_session()["dispatch"].dry_run
    server = text_interface.start(
        cfg, secrets, log, operations=FakeOperations(), media=FakeMedia())
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
        acquired = request(base + "/v1/chat", token, {
            "session": "couch", "message": "download Andor season 1"})[1]
        assert acquired["reply"] == (
            "Requested Andor, season 1, in 2160p. "
            "Sonarr is searching in the background.")
        assert first["turn"] != second["turn"]
        assert len(log.find("text_request")) == 3
        calls = log.find("tool_call")
        assert len(calls) == 2
        assert calls[0]["tool"] == "list_operations" and calls[0]["ok"]
        assert calls[1]["tool"] == "request_series" and calls[1]["ok"]
    finally:
        server.shutdown()
        server.server_close()
        assistant_repl.BACKENDS["anthropic"] = original
    print("OK - text interface: bearer auth, health, and session continuity")


if __name__ == "__main__":
    main()
