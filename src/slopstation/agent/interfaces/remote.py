"""MCP server for Claude connectors: one tool over the text interface.

Hand-rolled and stdlib-only: the tool surface is one conversational call, so
the protocol is three methods over JSON-RPC. Ignores Mcp-Session-Id - the
client does not reliably echo it, and conversation identity rides in the tool
arguments instead (see `session`).
"""

import hmac
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slopstation import config

MAX_BODY = 64 * 1024
# claude.ai gives a tool call 300 s and progress notifications do not extend
# it; the rest of the budget is ours to answer in.
INNER_TIMEOUT_S = 280
LOCKOUT_AFTER = 5
LOCKOUT_S = 900
RATE_LIMIT = 30
RATE_WINDOW_S = 60
# A refused body is drained first, up to this much: closing while the
# client is still writing RSTs on Windows, and it then reads an aborted
# connection instead of the status we sent.
DRAIN_MAX = 1024 * 1024

# Newest first. initialize echoes the client's version when we speak it.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

TOOL_NAME = "ask_slopstation"
TOOL_DESCRIPTION = (
    "Talk to Slopstation, the assistant that runs the user's living-room "
    "system. Use it for anything about that system: downloading or queueing "
    "movies and TV shows, checking download or import status, the Steam game "
    "library, launching or quitting a game, the TV, and questions like "
    '"is the house up" or "did anything break". It reaches real hardware and '
    "real download services.\n\n"
    "Send a SELF-CONTAINED request. Slopstation cannot see this conversation, "
    "so resolve references before sending: \"for the It's Always Sunny "
    'request just made, only season 3", never "just season 3".\n\n'
    "Each reply ends with a session id. Pass that exact value back as "
    "`session` on later calls in this conversation so Slopstation keeps its "
    "context; omit it to start fresh."
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The self-contained request, in plain English.",
        },
        "session": {
            "type": "string",
            "description": "The session id from an earlier reply in this "
            "conversation. Omit it on the first call.",
        },
    },
    "required": ["message"],
}


def _result(rid, value):
    return {"jsonrpc": "2.0", "id": rid, "result": value}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class RemoteApplication:
    """Authenticated forwarder to /v1/chat. Holds no assistant state - the
    text interface owns sessions, tools, and the model."""

    def __init__(self, url, token, log):
        self.url = url.rstrip("/") + "/v1/chat"
        self.token = token
        self.log = log
        self.lock = threading.Lock()
        self.failures = 0
        self.locked_until = 0.0
        self.hits = []

    def locked(self):
        with self.lock:
            return time.monotonic() < self.locked_until

    def note_auth(self, ok):
        """Consecutive bad tokens shut the door; one good token clears it."""
        with self.lock:
            if ok:
                self.failures = 0
                return
            self.failures += 1
            if self.failures < LOCKOUT_AFTER:
                return
            self.failures = 0
            self.locked_until = time.monotonic() + LOCKOUT_S
        self.log.error("remote_lockout", failures=LOCKOUT_AFTER, lock_s=LOCKOUT_S)

    def throttled(self):
        with self.lock:
            now = time.monotonic()
            self.hits = [t for t in self.hits if now - t < RATE_WINDOW_S]
            if len(self.hits) >= RATE_LIMIT:
                return True
            self.hits.append(now)
            return False

    def ask(self, message, session=None):
        payload = {"message": message}
        if session:
            payload["session"] = session
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=INNER_TIMEOUT_S) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error")
            except (ValueError, UnicodeDecodeError):
                detail = f"HTTP {e.code}"
            raise RuntimeError(str(detail)) from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise RuntimeError("could not reach the text interface") from e
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError("assistant request failed")
        session_id = str(result.get("session", ""))
        self.log(
            "remote_request",
            turn=result.get("turn"),
            session=session_id,
            dur_ms=int((time.monotonic() - started) * 1000),
        )
        reply = str(result.get("reply", ""))
        if session_id:
            reply += (
                f"\n\n[session: {session_id} - pass this exact value as "
                "`session` on follow-up calls in this conversation]"
            )
        return reply

    def rpc(self, message):
        """One JSON-RPC request -> a response, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        if "id" not in message:
            return None
        rid = message["id"]
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            asked = str(params.get("protocolVersion", ""))
            return _result(
                rid,
                {
                    "protocolVersion": (
                        asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "slopstation", "version": "1"},
                },
            )
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(
                rid,
                {
                    "tools": [
                        {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "inputSchema": TOOL_SCHEMA,
                        }
                    ]
                },
            )
        if method == "tools/call":
            return self.call_tool(rid, params)
        return _error(rid, -32601, f"unknown method {method!r}")

    def call_tool(self, rid, params):
        if params.get("name") != TOOL_NAME:
            return _error(rid, -32602, f"unknown tool {params.get('name')!r}")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        message = str(args.get("message", "")).strip()
        if not message:
            return _error(rid, -32602, "message is required")
        session = args.get("session")
        try:
            reply = self.ask(message, str(session) if session else None)
        except RuntimeError as e:
            # A forwarding failure is a tool RESULT, not a protocol error: the
            # model should see it and be able to retry.
            self.log.error("remote_request_failed", err=str(e))
            return _result(
                rid,
                {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": f"Slopstation could not answer: {e}"}
                    ],
                },
            )
        return _result(rid, {"content": [{"type": "text", "text": reply}]})


class RemoteHandler(BaseHTTPRequestHandler):
    server: "RemoteServer"
    server_version = "SlopstationRemote/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _json(self, status, value):
        body = b"" if value is None else json.dumps(value).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self):
        # Bytes, not str: compare_digest RAISES on a non-ASCII header, and an
        # unanswered request would also skip the lockout count below.
        return hmac.compare_digest(
            self.headers.get("Authorization", "").encode("utf-8", "replace"),
            ("Bearer " + self.server.token).encode("utf-8"),
        )

    def _declared_length(self):
        """Content-Length as sent, or None when absent or unparseable."""
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None

    def _drain(self, length):
        remaining = min(length, DRAIN_MAX)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _mcp_path(self):
        return self.path.split("?")[0].rstrip("/") in ("", "/mcp")

    def do_GET(self):
        # The optional SSE stream. 405 is how a server declines it.
        self._json(405 if self._mcp_path() else 404, {"error": "not supported"})

    def do_POST(self):
        app = self.server.app
        # Bound, not app.log.warn(...): the event-name scan reads this shape.
        log = app.log
        length = self._declared_length()
        if length is not None and length > MAX_BODY:
            self._drain(length)
            self.close_connection = True
            self._json(413, {"error": f"body must be at most {MAX_BODY} bytes"})
            return
        if not self._mcp_path():
            self.close_connection = True
            self._json(404, {"error": "not found"})
            return
        if length is None or length <= 0:
            # Nothing was read from the socket, so the stream position is
            # unknown: close rather than let the unread body desync the next
            # request on a connection the client keeps alive.
            self.close_connection = True
            self._json(400, {"error": "body must carry a Content-Length"})
            return
        # Read before every early return below, so a refused request leaves
        # the connection reusable.
        raw = self.rfile.read(length)
        if app.locked():
            self._json(429, {"error": "locked out"})
            return
        ok = self._authorized()
        app.note_auth(ok)
        if not ok:
            log.warn("remote_auth_failed", reason="bad or missing token")
            self._json(401, {"error": "unauthorized"})
            return
        if app.throttled():
            log.warn("remote_throttled", limit=RATE_LIMIT)
            self._json(429, {"error": "rate limit"})
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._json(200, _error(None, -32700, "parse error"))
            return
        if isinstance(message, list):
            self._json(200, _error(None, -32600, "batches are not supported"))
            return
        try:
            response = app.rpc(message)
        except Exception as e:
            log.error("remote_request_failed", err=str(e))
            self._json(200, _error(message.get("id"), -32603, "internal error"))
            return
        if response is None:
            self._json(202, None)
            return
        self._json(200, response)


class RemoteServer(ThreadingHTTPServer):
    app: RemoteApplication
    token: str

    def handle_error(self, request, client_address):
        # Refusing a body closes the socket mid-write, which the client sees
        # as an abort. That is the design; only real faults deserve a console.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def start(cfg, secrets, log):
    remote_cfg = cfg.get("remoteInterface") or {}
    if not remote_cfg.get("enabled"):
        return None
    token = secrets.get("remoteInterfaceToken")
    if not config.real_key(token):
        log.warn(
            "lane_disabled",
            what="remote_interface",
            reason="remoteInterfaceToken is missing or a placeholder",
        )
        return None
    text_cfg = cfg.get("textInterface") or {}
    inner_token = secrets.get("textInterfaceToken")
    if not (text_cfg.get("enabled") and config.real_key(inner_token)):
        log.warn(
            "lane_disabled",
            what="remote_interface",
            reason="the text interface it forwards to is not enabled",
        )
        return None
    inner_host = str(text_cfg.get("host", "127.0.0.1"))
    if inner_host in ("0.0.0.0", "::"):
        inner_host = "127.0.0.1"
    inner_url = f"http://{inner_host}:{int(text_cfg.get('port', 8765))}"
    host = str(remote_cfg.get("host", "127.0.0.1"))
    port = int(remote_cfg.get("port", 8766))
    app = RemoteApplication(inner_url, str(inner_token), log)
    try:
        server = RemoteServer((host, port), RemoteHandler)
    except OSError as e:
        log.warn("lane_disabled", what="remote_interface", reason=str(e))
        return None
    server.app = app
    server.token = str(token)
    threading.Thread(
        target=server.serve_forever, daemon=True, name="remote-interface"
    ).start()
    log("lane_up", what="remote_interface", host=host, port=server.server_address[1])
    return server
