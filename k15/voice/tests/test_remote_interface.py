"""Blind test: the MCP wrapper's protocol, forwarding, auth, and lockout."""
import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _bootstrap

import cglib
import remote_interface

INNER_TOKEN = "i" * 64
OUTER_TOKEN = "o" * 64


class FakeChat(BaseHTTPRequestHandler):
    """Stands in for /v1/chat: echoes the message and mints a session."""

    def log_message(self, format, *args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(
            dict(body, auth=self.headers.get("Authorization")))
        payload = json.dumps({
            "ok": True, "session": body.get("session") or "minted1",
            "turn": "abc123", "reply": "queued " + body["message"],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def rpc(base, payload, token=OUTER_TOKEN):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        base + "/mcp", data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def call(base, arguments, rid=9):
    return rpc(base, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                      "params": {"name": "ask_slopstation",
                                 "arguments": arguments}})


def main():
    inner = ThreadingHTTPServer(("127.0.0.1", 0), FakeChat)
    inner.seen = []
    threading.Thread(target=inner.serve_forever, daemon=True).start()

    cfg = json.loads(json.dumps(_bootstrap.CONFIG))
    cfg["textInterface"] = {"enabled": True, "host": "127.0.0.1",
                            "port": inner.server_address[1]}
    cfg["remoteInterface"] = {"enabled": True, "host": "127.0.0.1", "port": 0}
    secrets = {"textInterfaceToken": INNER_TOKEN,
               "remoteInterfaceToken": OUTER_TOKEN}
    log = cglib.CapturingLog("voice")

    disabled = remote_interface.start(
        cfg, {"remoteInterfaceToken": OUTER_TOKEN}, log)
    assert disabled is None, "started without a reachable text interface"
    assert log.find("lane_disabled")[0]["what"] == "remote_interface"

    server = remote_interface.start(cfg, secrets, log)
    assert server is not None
    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"
        assert log.find("lane_up")[0]["what"] == "remote_interface"

        status, hello = rpc(base, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"}})
        assert status == 200
        assert hello["result"]["protocolVersion"] == "2025-06-18"
        assert hello["result"]["serverInfo"]["name"] == "slopstation"
        assert "tools" in hello["result"]["capabilities"]

        unknown = rpc(base, {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                             "params": {"protocolVersion": "1999-01-01"}})[1]
        assert unknown["result"]["protocolVersion"] == \
            remote_interface.PROTOCOL_VERSIONS[0]

        status, body = rpc(base, {"jsonrpc": "2.0",
                                  "method": "notifications/initialized"})
        assert status == 202 and body is None, "notification got a response"

        listed = rpc(base, {"jsonrpc": "2.0", "id": 3,
                            "method": "tools/list"})[1]["result"]["tools"]
        assert len(listed) == 1 and listed[0]["name"] == "ask_slopstation"
        schema = listed[0]["inputSchema"]
        assert set(schema["properties"]) == {"message", "session"}
        assert schema["required"] == ["message"]

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

        empty = call(base, {"message": "  "})[1]
        assert empty["error"]["code"] == -32602
        assert call(base, {"message": "hi"}, rid=4)[1]["id"] == 4
        nomethod = rpc(base, {"jsonrpc": "2.0", "id": 5, "method": "nope"})[1]
        assert nomethod["error"]["code"] == -32601

        inner.shutdown()
        inner.server_close()
        down = call(base, {"message": "still there?"})[1]["result"]
        assert down["isError"] and "could not" in down["content"][0]["text"]
        assert len(log.find("remote_request_failed")) == 1

        # One connection, three exchanges: a body, an empty 202, a body.
        # Wrong Content-Length would desync the stream rather than fail loudly.
        keep = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Authorization": "Bearer " + OUTER_TOKEN,
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-11-25"}
        for payload, expect in (
                ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, 200),
                ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 202),
                ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 200)):
            keep.request("POST", "/mcp", json.dumps(payload), headers)
            response = keep.getresponse()
            body = response.read()
            assert response.status == expect, (payload, response.status)
            assert response.getheader("Connection") != "close"
            if expect == 200:
                assert json.loads(body)["id"] == payload["id"]
        keep.close()

        # A refused body is drained, answered, then closed. Without the drain
        # the close RSTs mid-send and the client reads an abort, not the 413.
        big = http.client.HTTPConnection(host, port, timeout=5)
        big.request("POST", "/mcp", "x" * (remote_interface.MAX_BODY + 1),
                    dict(headers, **{"Content-Type": "application/json"}))
        oversized = big.getresponse()
        oversized.read()
        assert oversized.status == 413
        assert oversized.getheader("Connection") == "close"
        big.close()

        ping = {"jsonrpc": "2.0", "id": 6, "method": "ping"}

        # The rate cap, driven at a small limit so the real one stays unspent.
        real_limit = remote_interface.RATE_LIMIT
        remote_interface.RATE_LIMIT = 3
        server.app.hits.clear()          # the window already holds this test
        try:
            codes = [rpc(base, ping)[0] for _ in range(4)]
        finally:
            remote_interface.RATE_LIMIT = real_limit
        assert codes == [200, 200, 200, 429], codes
        assert len(log.find("remote_throttled")) == 1

        assert rpc(base, ping, token="wrong")[0] == 401
        assert len(log.find("remote_auth_failed")) == 1
        assert rpc(base, ping, token=None)[0] == 401

        # A non-ASCII header must not escape the auth check: compare_digest
        # raises on str, and an unanswered request skips the lockout count.
        odd = http.client.HTTPConnection(host, port, timeout=5)
        odd.request("POST", "/mcp", json.dumps(ping),
                    {"Authorization": "Bearer ü",
                     "Content-Type": "application/json"})
        refused = odd.getresponse()
        refused.read()
        assert refused.status == 401, "non-ASCII header was not answered"
        odd.close()
        assert len(log.find("remote_auth_failed")) == 3, "the odd one uncounted"

        # A refusal leaves the connection reusable: the body is read first.
        reused = http.client.HTTPConnection(host, port, timeout=5)
        for token, expect in (("wrong", 401), (OUTER_TOKEN, 200)):
            reused.request("POST", "/mcp", json.dumps(ping),
                           {"Authorization": "Bearer " + token,
                            "Content-Type": "application/json"})
            response = reused.getresponse()
            response.read()
            assert response.status == expect, (token, response.status)
        reused.close()

        # That last good token cleared the counter, so this drives it cleanly.
        for _ in range(remote_interface.LOCKOUT_AFTER):
            rpc(base, ping, token="wrong")
        lockout = log.find("remote_lockout")
        assert len(lockout) == 1, "lockout did not fire on the fifth failure"
        assert lockout[0]["lock_s"] == remote_interface.LOCKOUT_S
        assert rpc(base, ping)[0] == 429
    finally:
        server.shutdown()
        server.server_close()
    print("OK - remote interface: MCP handshake, forwarding, auth lockout")


if __name__ == "__main__":
    main()
