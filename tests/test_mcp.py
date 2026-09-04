"""The MCP wrapper's protocol, forwarding, auth, and lockout."""

import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import helpers
from slopstation.agent.interfaces import mcp

INNER_TOKEN = "i" * 64
OUTER_TOKEN = "o" * 64
SECRETS = {"textInterfaceToken": INNER_TOKEN, "remoteInterfaceToken": OUTER_TOKEN}

PING = {"jsonrpc": "2.0", "id": 6, "method": "ping"}
# What a real MCP client sends on a kept-alive connection.
HEADERS = {
    "Authorization": "Bearer " + OUTER_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


class FakeChat(BaseHTTPRequestHandler):
    """Stands in for /v1/chat: echoes the message and mints a session."""

    server: "FakeChatServer"

    def log_message(self, format, *args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(dict(body, auth=self.headers.get("Authorization")))
        payload = json.dumps(
            {
                "ok": True,
                "session": body.get("session") or "minted1",
                "turn": "abc123",
                "reply": "queued " + body["message"],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeChatServer(ThreadingHTTPServer):
    """The text interface the wrapper forwards to, on a free port, recording
    every request it was sent."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeChat)
        self.seen: list[dict] = []


def rpc(base, payload, token=OUTER_TOKEN):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def call(base, arguments, rid=9):
    return rpc(
        base,
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "ask_slopstation", "arguments": arguments},
        },
    )


def connection(server):
    """A raw keep-alive connection, for the exchanges urllib would hide."""
    host, port = server.server_address
    return http.client.HTTPConnection(host, port, timeout=5)


@pytest.fixture
def inner():
    server = FakeChatServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    # A test may have taken it down already, to prove the wrapper copes;
    # both calls are idempotent.
    server.shutdown()
    server.server_close()


@pytest.fixture
def cfg(inner):
    cfg = json.loads(json.dumps(helpers.CONFIG))
    cfg["textInterface"] = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": inner.server_address[1],
    }
    cfg["remoteInterface"] = {"enabled": True, "host": "127.0.0.1", "port": 0}
    return cfg


@pytest.fixture
def server(cfg, log):
    """The MCP wrapper on a free port, forwarding to `inner`."""
    server = mcp.start(cfg, SECRETS, log)
    assert server is not None
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def base(server):
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_start_needs_a_reachable_text_interface(cfg, log):
    disabled = mcp.start(cfg, {"remoteInterfaceToken": OUTER_TOKEN}, log)
    assert disabled is None, "started without a reachable text interface"
    assert log.find("lane_disabled")[0]["what"] == "remote_interface"


def test_initialize_negotiates_the_protocol_version(base):
    status, hello = rpc(
        base,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert status == 200
    assert hello["result"]["protocolVersion"] == "2025-06-18"
    assert hello["result"]["serverInfo"]["name"] == "slopstation"
    assert "tools" in hello["result"]["capabilities"]

    unknown = rpc(
        base,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        },
    )[1]
    assert unknown["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_a_notification_gets_an_empty_202(base):
    status, body = rpc(base, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None, "notification got a response"


def test_tools_list_exposes_the_one_tool(base):
    listed = rpc(base, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})[1][
        "result"
    ]["tools"]
    assert len(listed) == 1 and listed[0]["name"] == "ask_slopstation"
    schema = listed[0]["inputSchema"]
    assert set(schema["properties"]) == {"message", "session"}
    assert schema["required"] == ["message"]


def test_a_tool_call_forwards_and_carries_the_session(base, inner, log):
    first = call(base, {"message": "download always sunny"})[1]["result"]
    assert not first.get("isError")
    text = first["content"][0]["text"]
    assert "queued download always sunny" in text
    assert "minted1" in text, "the session id never reached the model"
    assert inner.seen[0]["auth"] == "Bearer " + INNER_TOKEN
    assert "session" not in inner.seen[0], "invented a session id"
    request = log.find("remote_request")[0]
    assert request["turn"] == "abc123" and request["session"] == "minted1"
    assert request["dur_ms"] >= 0

    call(base, {"message": "only season 3", "session": "minted1"})
    assert inner.seen[1]["session"] == "minted1"


def test_bad_calls_are_jsonrpc_errors(base):
    empty = call(base, {"message": "  "})[1]
    assert empty["error"]["code"] == -32602
    assert call(base, {"message": "hi"}, rid=4)[1]["id"] == 4
    nomethod = rpc(base, {"jsonrpc": "2.0", "id": 5, "method": "nope"})[1]
    assert nomethod["error"]["code"] == -32601


def test_an_unreachable_text_interface_is_a_tool_result(base, inner, log):
    inner.shutdown()
    inner.server_close()
    down = call(base, {"message": "still there?"})[1]["result"]
    assert down["isError"] and "could not" in down["content"][0]["text"]
    assert len(log.find("remote_request_failed")) == 1


def test_one_connection_survives_three_exchanges(server):
    # One connection, three exchanges: a body, an empty 202, a body.
    # Wrong Content-Length would desync the stream rather than fail loudly.
    keep = connection(server)
    for payload, expect in (
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, 200),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 202),
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 200),
    ):
        keep.request("POST", "/mcp", json.dumps(payload), HEADERS)
        response = keep.getresponse()
        body = response.read()
        assert response.status == expect, (payload, response.status)
        assert response.getheader("Connection") != "close"
        if expect == 200:
            assert json.loads(body)["id"] == payload["id"]
    keep.close()


def test_an_oversized_body_is_drained_then_refused(server):
    # A refused body is drained, answered, then closed. Without the drain
    # the close RSTs mid-send and the client reads an abort, not the 413.
    big = connection(server)
    big.request("POST", "/mcp", "x" * (mcp.MAX_BODY + 1), HEADERS)
    oversized = big.getresponse()
    oversized.read()
    assert oversized.status == 413
    assert oversized.getheader("Connection") == "close"
    big.close()


def test_the_rate_cap(base, log, monkeypatch):
    # The rate cap, driven at a small limit so the real one stays unspent.
    monkeypatch.setattr(mcp, "RATE_LIMIT", 3)
    codes = [rpc(base, PING)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429], codes
    assert len(log.find("remote_throttled")) == 1


def test_a_bad_token_is_refused_and_counted(base, server, log):
    assert rpc(base, PING, token="wrong")[0] == 401
    assert len(log.find("remote_auth_failed")) == 1
    assert rpc(base, PING, token=None)[0] == 401

    # A non-ASCII header must not escape the auth check: compare_digest
    # raises on str, and an unanswered request skips the lockout count.
    odd = connection(server)
    odd.request(
        "POST",
        "/mcp",
        json.dumps(PING),
        {"Authorization": "Bearer ü", "Content-Type": "application/json"},
    )
    refused = odd.getresponse()
    refused.read()
    assert refused.status == 401, "non-ASCII header was not answered"
    odd.close()
    assert len(log.find("remote_auth_failed")) == 3, "the odd one uncounted"


def test_a_refusal_leaves_the_connection_reusable(server):
    # A refusal leaves the connection reusable: the body is read first.
    reused = connection(server)
    for token, expect in (("wrong", 401), (OUTER_TOKEN, 200)):
        reused.request(
            "POST",
            "/mcp",
            json.dumps(PING),
            {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        response = reused.getresponse()
        response.read()
        assert response.status == expect, (token, response.status)
    reused.close()


def test_consecutive_failures_lock_the_door(base, log):
    # One good token clears the counter, so the failures before it do not
    # count toward the lockout: it fires on the fifth of the run after it.
    for _ in range(mcp.LOCKOUT_AFTER - 1):
        rpc(base, PING, token="wrong")
    assert rpc(base, PING)[0] == 200
    for _ in range(mcp.LOCKOUT_AFTER):
        rpc(base, PING, token="wrong")
    lockout = log.find("remote_lockout")
    assert len(lockout) == 1, "lockout did not fire on the fifth failure"
    assert lockout[0]["lock_s"] == mcp.LOCKOUT_S
    assert rpc(base, PING)[0] == 429
