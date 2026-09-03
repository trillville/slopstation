"""Authenticated LAN text interface over the production assistant tools."""

import hmac
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slopstation import config
from slopstation.agent.brain import assistant, backends
from slopstation.agent.brain.dispatch import Dispatch
from slopstation.agent.telemetry import traces

MAX_BODY = 64 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Grace, not a queue: a second turn on a session waits this long for the
# first, then 503s. Blocking forever leaks one handler thread per retry
# while a stalled turn holds the session lock.
BUSY_GRACE_S = 2.0


class SessionBusy(RuntimeError):
    """The session's previous turn is still running."""


class TextApplication:
    def __init__(
        self, cfg, secrets, log, operations=None, steam=None, media=None, dry_run=False
    ):
        self.cfg = cfg
        self.secrets = secrets
        self.log = log
        self.operations = operations
        self.steam = steam
        self.media = media
        self.dry_run = dry_run
        self.voice = cfg["voice"]
        self.provider = self.voice["assistantProvider"]
        self.system_text = assistant.system_instruction(cfg, interface="text")
        self.sessions = OrderedDict()
        self.lock = threading.Lock()

    def _new_session(self):
        backend_type = backends.BACKENDS[self.provider]
        model = assistant.default_model(self.voice, self.provider)
        backend = backend_type(
            self.secrets,
            model,
            effort=self.voice["assistantReasoningEffort"],
            voice=self.voice,
        )
        dispatch = Dispatch(self.cfg, self.log, dry_run=self.dry_run)
        impls = assistant.tool_impls(
            dispatch,
            self.log,
            operations=self.operations,
            voice=self.voice,
            steam=self.steam,
            media=self.media,
        )
        return {
            "backend": backend,
            "dispatch": dispatch,
            "impls": impls,
            "lock": threading.Lock(),
            # Taken once, so every turn of this conversation rewrites the
            # one trace file rather than adding another.
            "stem": time.strftime("%Y%m%d-%H%M%S"),
        }

    def turn(self, session_id, message):
        if not SESSION_RE.fullmatch(session_id):
            raise ValueError(
                "session must use 1-64 letters, digits, dashes, or underscores"
            )
        message = str(message).strip()
        if not message or len(message) > 8000:
            raise ValueError("message must contain 1-8000 characters")
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                if len(self.sessions) >= 32:
                    self.sessions.popitem(last=False)
                session = self._new_session()
                self.sessions[session_id] = session
            else:
                self.sessions.move_to_end(session_id)
        turn = uuid.uuid4().hex[:6]
        started = time.monotonic()
        if not session["lock"].acquire(timeout=BUSY_GRACE_S):
            self.log.warn("text_session_busy", session=session_id)
            raise SessionBusy(
                "this session is still answering its previous message; "
                "retry shortly or start a new session"
            )
        try:
            session["dispatch"].begin_utterance(turn, message)
            impls = {}
            acknowledgments = []
            for name, fn in session["impls"].items():

                def audited(args, _name=name, _fn=fn):
                    # The raise is load-bearing here: the LAN client learns a
                    # tool failed as a 500. The voice lane fail-softs instead,
                    # because an uncalled result_callback breaks the turn.
                    try:
                        out = _fn(args)
                    except Exception:
                        assistant.record_tool_call(_name, args, {"ok": False}, self.log)
                        raise
                    assistant.record_tool_call(_name, args, out, self.log)
                    if isinstance(out, dict) and out.get("acknowledgment"):
                        acknowledgments.append(str(out["acknowledgment"]))
                    return out

                impls[name] = audited
            reply = session["backend"].turn(self.system_text, message, impls)
            if acknowledgments:
                reply = acknowledgments[-1]
            # Copied under the lock: a concurrent turn on this session appends
            # to the same list. The MCP adapter forwards to here, so its turns
            # trace too - remote_request carries the same turn id.
            messages = list(getattr(session["backend"], "messages", ()))
        finally:
            session["lock"].release()
        self.log(
            "text_request",
            turn=turn,
            session=session_id,
            dur_ms=int((time.monotonic() - started) * 1000),
        )
        traces.save(
            "text",
            messages,
            meta={"session": session_id, "turn": turn},
            stem=session["stem"],
        )
        return {"ok": True, "session": session_id, "turn": turn, "reply": reply}


class TextServer(ThreadingHTTPServer):
    app: TextApplication
    token: str


class TextHandler(BaseHTTPRequestHandler):
    server: TextServer
    server_version = "SlopstationText/1"

    def log_message(self, format, *args):
        return

    def _json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        return hmac.compare_digest(supplied, expected)

    def do_POST(self):
        if self.path != "/v1/chat":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("request body is empty or too large")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            session_id = str(value.get("session") or uuid.uuid4().hex)
            result = self.server.app.turn(session_id, value.get("message", ""))
            self._json(200, result)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            self._json(400, {"ok": False, "error": str(e)})
        except SessionBusy as e:
            # 503 with the reason: remote.py surfaces an HTTP error's
            # `error` field to the caller, so the busy text reaches the app.
            self._json(503, {"ok": False, "error": str(e)})
        except Exception as e:
            self.server.app.log.error("text_request_failed", err=str(e))
            self._json(500, {"ok": False, "error": "assistant request failed"})


def start(cfg, secrets, log, operations=None, steam=None, media=None, dry_run=False):
    text_cfg = cfg.get("textInterface") or {}
    if not text_cfg.get("enabled"):
        return None
    token = secrets.get("textInterfaceToken")
    if not config.real_key(token):
        log.warn(
            "lane_disabled",
            what="text_interface",
            reason="textInterfaceToken is missing or a placeholder",
        )
        return None
    provider = cfg["voice"]["assistantProvider"]
    key = assistant.PROVIDER_KEY[provider]
    if not config.real_key(secrets.get(key)):
        log.warn(
            "lane_disabled",
            what="text_interface",
            reason=f"{key} is missing or a placeholder",
        )
        return None
    host = str(text_cfg.get("host", "127.0.0.1"))
    port = int(text_cfg.get("port", 8765))
    app = TextApplication(cfg, secrets, log, operations, steam, media, dry_run)
    try:
        server = TextServer((host, port), TextHandler)
    except OSError as e:
        log.warn("lane_disabled", what="text_interface", reason=str(e))
        return None
    server.app = app
    server.token = str(token)
    threading.Thread(
        target=server.serve_forever, daemon=True, name="text-interface"
    ).start()
    log(
        "lane_up",
        what="text_interface",
        host=host,
        port=server.server_address[1],
        dry_run=dry_run or None,
    )
    return server
