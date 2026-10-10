"""Direct API calls to any service the house has credentials for.

The last resort after find_tools misses. Reads go through freely. Every
mutation goes through the confirm gate with the literal request shown, since
nobody anticipated the call. A short blocklist refuses operator settings
outright. Responses are scrubbed of anything secret-shaped before the model
sees them, and every call is logged as a tool_gap so the curated library
grows from what people actually reach for.
"""

from __future__ import annotations

import json
import re
from typing import Any

from slopstation.agent import operations as operations_mod
from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
from slopstation.agent.media import apidocs
from slopstation.agent.steam import library, session

METHODS = ("GET", "POST", "PUT", "DELETE")
# Status text the media clients raise for an answered-and-refused request.
HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


class HttpFailure(Exception):
    """The service answered, and refused: the status, the body it sent, and
    the refusal in words (an HTTP status, or Steam's own result code on a
    200)."""

    def __init__(self, status, body, reason=None):
        super().__init__(reason or f"answered HTTP {status}")
        self.status = status
        self.body = body
        self.reason = reason or f"answered HTTP {status}"


# No empty segments: the blocklist is a string match, so `config//host` must
# not read differently from `config/host`.
PATH_RE = re.compile(r"^[A-Za-z0-9_.\-{}]+(/[A-Za-z0-9_.\-{}]+)*$")
MAX_RESULT_CHARS = 8000
MAX_FIELDS = 30
# How many of a cut response's key names to list, so the model can pick
# `fields` from what is there instead of guessing.
MAX_KEYS_SHOWN = 40

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|password|passwd|secret|passkey|authorization|cookie|magnet)",
    re.I,
)
# Private trackers put the account passkey in the announce URL; a magnet link
# carries every tracker URL-encoded.
TRACKER_RE = re.compile(
    r"^(udp|wss?|https?)://\S*(announce|scrape|passkey|authkey)|^magnet:", re.I
)
# A credential inside a longer string: a query parameter, a URL, an error.
SECRET_PARAM_RE = re.compile(
    r"(?i)\b(key|api_?key|access_token|token|passkey|authkey|password)=[^&\s\"']+"
)
# Servarr provider resources carry credentials as fields [{name, value}].
SECRET_PRIVACY = ("password", "apikey")

# Operator settings: changed at a keyboard, not from the couch. Prefixes,
# matched against the path with its leading slash stripped.
ARR_BLOCKED_WRITE = (
    "system",
    "config",
    "downloadclient",
    "indexer",
    "importlist",
    "notification",
    "rootfolder",
    "qualityprofile",
    "customformat",
    "delayprofile",
    "remotepathmapping",
    "metadata",
    "autotagging",
    "customfilter",
    "tag",
    "applications",
    "appprofile",
    "indexerproxy",
    "development",
)
# Reads that are settings pages or credential stores in their own right.
ARR_BLOCKED_READ = (
    "config",
    "system/backup",
    "downloadclient",
    "notification",
    "applications",
    "indexerproxy",
)
# Commands that restart or rewrite the app rather than act on media.
ARR_BLOCKED_COMMANDS = ("applicationupdate", "backup", "restart", "reset")
# Writes a curated tool already does, with the house's defaults and no
# confirmation round trip: (service, method, path) -> the tool to use.
CURATED = {
    ("sonarr", "POST", "series"): "request_series",
    ("radarr", "POST", "movie"): "request_movie",
}
QBIT_BLOCKED = (
    "app/shutdown",
    "app/setPreferences",
    "app/setCookies",
    "rss",
    "search/installPlugin",
    "search/uninstallPlugin",
    "search/updatePlugins",
    "torrents/removeCategories",
    "torrents/deleteTags",
)
# qBittorrent runs the same action for GET and POST (only a newer server
# answers 405 for the wrong verb), so the method the model wrote is not what
# decides whether a call mutates. Everything outside this read set is gated.
QBIT_READS = (
    "torrents/info",
    "torrents/properties",
    "torrents/files",
    "torrents/trackers",
    "torrents/webseeds",
    "torrents/pieceStates",
    "torrents/pieceHashes",
    "torrents/categories",
    "torrents/tags",
    "torrents/count",
    "torrents/export",
    "transfer/info",
    "transfer/speedLimitsMode",
    "transfer/downloadLimit",
    "transfer/uploadLimit",
    "app/version",
    "app/webapiVersion",
    "app/buildInfo",
    "app/preferences",
    "app/defaultSavePath",
    "app/networkInterfaceList",
    "app/networkInterfaceAddressList",
    "sync/maindata",
    "sync/torrentPeers",
    "log/main",
    "log/peers",
    "search/status",
    "search/results",
    "search/plugins",
)
STEAM_HOSTS = ("api.steampowered.com", "store.steampowered.com", "steamcommunity.com")

_RESEARCH = """\
Research first: call describe_api for the service and topic, and web search
when that is silent. Never guess a path or a body. GET runs at once. Any
other method returns the literal request for the user to confirm: read it
back in plain words and call again unchanged once they say yes."""

DESCRIBE_API = """\
Read a service's own API documentation for one topic, live and current.
service: radarr, sonarr, prowlarr (OpenAPI: paths, methods, parameters, body
fields) or qbittorrent (its wiki: sections). topic: a path fragment or a word
such as 'queue', 'release', 'torrents/info', 'transfer'. Call this before
any *_api call whose shape you are not sure of. Steam has no fetchable
document: use web search for it."""

RADARR_API = (
    "Any Radarr v3 call: method, path under /api/v3, query params, JSON body. "
    "Adding a movie is request_movie, never POST /movie here. " + _RESEARCH
)
SONARR_API = (
    "Any Sonarr v3 call: method, path under /api/v3, query params, JSON body. "
    "Adding a series is request_series, never POST /series here. " + _RESEARCH
)
PROWLARR_API = (
    "Any Prowlarr v1 call: method, path under /api/v1, query params, JSON body. "
    + _RESEARCH
)
QBITTORRENT_API = (
    "Any qBittorrent v2 call on the authenticated session: method, path under "
    "/api/v2 (e.g. torrents/info, transfer/info), query params for GET, form "
    "fields as `body` for POST. A loaded tool that already answers comes "
    "first: seeding_report carries the share limits and global policy, "
    "transfer_info the speeds. A path that is not a documented read is "
    "treated as an action and asked about, so never invent one. " + _RESEARCH
)
STEAM_API = """\
Any Steam web call: GET or POST to api.steampowered.com (pass the interface
path, e.g. ISteamUserStats/GetPlayerAchievements/v1), store.steampowered.com
or steamcommunity.com (pass the path). auth 'key' adds the Web API key,
'account' adds the signed-in account's token (needed for IClientCommService
and other account-scoped calls), 'none' sends neither. Research the call
first with web search; never guess. POST returns the literal request for the
user to confirm before it runs."""


def _params_schema(extra=None):
    props = {
        "method": {"type": "string", "enum": list(METHODS)},
        "path": {"type": "string", "description": "path under the API root"},
        "params": {
            "type": "object",
            "description": "query parameters",
            "additionalProperties": True,
        },
        "body": {
            "description": "request body: a JSON object, or a JSON array where "
            "the API takes one (manualimport); form fields for qBittorrent",
            "anyOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "array", "items": {}},
            ],
        },
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "keep only these keys of the response, at any depth, "
            "and the objects and lists that hold them, e.g. ['name', "
            "'displayName']. For a response too big to read whole.",
        },
    }
    props.update(extra or {})
    return props


def _spec(name, description, needs, keywords, extra=None, busy=None):
    return ToolSpec(
        name,
        description,
        _params_schema(extra),
        ("method", "path"),
        risk="destructive",
        area="api",
        keywords=keywords,
        default=False,
        needs=needs,
        busy=busy,
    )


SPECS = [
    ToolSpec(
        "describe_api",
        DESCRIBE_API,
        {
            "service": {
                "type": "string",
                "enum": ["radarr", "sonarr", "prowlarr", "qbittorrent"],
            },
            "topic": {"type": "string", "description": "path fragment or keyword"},
        },
        ("service", "topic"),
        risk="read",
        area="api",
        keywords=("api documentation", "endpoint", "openapi", "how to call", "docs"),
        default=False,
        needs=("media",),
        busy="reading the docs",
    ),
    _spec(
        "radarr_api",
        RADARR_API,
        ("media",),
        ("radarr api", "raw radarr call", "movies api", "direct call"),
        busy="calling Radarr",
    ),
    _spec(
        "sonarr_api",
        SONARR_API,
        ("media",),
        ("sonarr api", "raw sonarr call", "series api", "direct call"),
        busy="calling Sonarr",
    ),
    _spec(
        "prowlarr_api",
        PROWLARR_API,
        ("prowlarr",),
        ("prowlarr api", "indexers api", "raw prowlarr call", "direct call"),
        busy="calling Prowlarr",
    ),
    _spec(
        "qbittorrent_api",
        QBITTORRENT_API,
        ("torrents",),
        ("qbittorrent api", "raw torrent call", "qbit", "direct call"),
        busy="calling qBittorrent",
    ),
    _spec(
        "steam_api",
        STEAM_API,
        ("steam_data",),
        ("steam api", "steam web api", "raw steam call", "direct call"),
        extra={"auth": {"type": "string", "enum": ["none", "key", "account"]}},
        busy="calling Steam",
    ),
]


def scrub(value):
    """Redact secret-shaped fields, provider credential fields, tracker and
    magnet URLs, and credentials inside strings, recursively."""
    if isinstance(value, dict):
        out = {}
        # {name: "apiKey", value: ...} / {privacy: "password", value: ...}
        field_secret = (
            SECRET_KEY_RE.search(str(value.get("name", "")))
            or str(value.get("privacy", "")).lower() in SECRET_PRIVACY
        )
        for k, v in value.items():
            if SECRET_KEY_RE.search(str(k)) or (field_secret and k == "value"):
                out[k] = "[redacted]"
            else:
                out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        if TRACKER_RE.match(value):
            return "[redacted tracker url]"
        return SECRET_PARAM_RE.sub(
            lambda m: m.group(0).split("=")[0] + "=[redacted]", value
        )
    return value


def _cap(result):
    text = json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result, False
    return text[:MAX_RESULT_CHARS] + " ...", True


_DROP = object()


def _select(value, keys):
    """Only the named keys, wherever they sit, and the containers on the way
    to them. _DROP when nothing under `value` matches."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in keys:
                out[k] = v
            elif (kept := _select(v, keys)) is not _DROP:
                out[k] = kept
        return out or _DROP
    if isinstance(value, list):
        kept = [x for x in (_select(v, keys) for v in value) if x is not _DROP]
        return kept or _DROP
    return _DROP


def _key_names(value, seen=None):
    """Every distinct key in a response, in the order first met."""
    seen = {} if seen is None else seen
    if isinstance(value, dict):
        for k, v in value.items():
            seen.setdefault(k, None)
            _key_names(v, seen)
    elif isinstance(value, list):
        for v in value:
            _key_names(v, seen)
    return list(seen)


def _under(path, prefixes):
    """Whole-segment prefix match: `tag` covers `tag` and `tag/3`, not `tags`."""
    p = path.lower()
    return any(p == b.lower() or p.startswith(b.lower() + "/") for b in prefixes)


def _blocked(service, method, path, body=None):
    if service in ("radarr", "sonarr", "prowlarr"):
        if method != "GET" and _under(path, ARR_BLOCKED_WRITE):
            return True
        if _under(path, ARR_BLOCKED_READ):
            return True
        if method != "GET" and _under(path, ("command",)) and isinstance(body, dict):
            if str(body.get("name", "")).lower() in ARR_BLOCKED_COMMANDS:
                return True
    if service == "qbittorrent":
        return _under(path, QBIT_BLOCKED)
    return False


def _qbit_mutates(method, path):
    return method != "GET" or not _under(path, QBIT_READS)


def impls(ctx: ToolContext):
    bind = Bindings(ctx, SPECS)
    log, media, steam, operations = ctx.log, ctx.media, ctx.steam, ctx.operations
    # The reads this turn has already had answered, and whether each answer
    # was cut. Asking again cannot change the answer, and a model that keeps
    # asking spends the user's wait on it (2026-10-10: one Steam call 59
    # times in one turn).
    answered: dict[str, Any] = {"turn": None, "reads": {}}

    def _run(service, args, send, tag=""):
        """`tag` names anything beyond method, path and body that the user is
        confirming (the Steam credential), so it is shown and in the scope."""
        method = str(args.get("method") or "GET").upper()
        path = str(args.get("path") or "").strip().strip("/")
        params = args.get("params") or {}
        body = args.get("body")
        if method not in METHODS:
            return {"ok": False, "error": f"method must be one of {', '.join(METHODS)}"}
        if not path or not PATH_RE.match(path) or ".." in path.split("/"):
            return {"ok": False, "error": "path must be a plain API path"}
        if not isinstance(params, dict):
            return {"ok": False, "error": "params must be an object"}
        if body is not None and not isinstance(body, (dict, list)):
            return {"ok": False, "error": "body must be a JSON object or array"}
        if service == "qbittorrent" and isinstance(body, list):
            return {"ok": False, "error": "qBittorrent takes form fields, not an array"}
        fields = args.get("fields")
        if fields is not None and not (
            isinstance(fields, list)
            and 0 < len(fields) <= MAX_FIELDS
            and all(isinstance(f, str) and f for f in fields)
        ):
            return {
                "ok": False,
                "error": f"fields must be a list of 1-{MAX_FIELDS} key names",
            }
        keys = frozenset(fields or ())
        if service == "qbittorrent" and _qbit_mutates(method, path):
            # An action is an action whatever verb the model wrote.
            method = "POST"
        if _blocked(service, method, path, body):
            log.warn(
                "tool_refused", tool=f"{service}_api", reason="blocklisted", path=path
            )
            return {
                "ok": False,
                "error": f"{method} /{path} is an operator setting and is not "
                "reachable from here",
            }
        curated = CURATED.get((service, method, path.lower()))
        if curated:
            log.warn("tool_refused", tool=f"{service}_api", reason="curated", path=path)
            return {
                "ok": False,
                "error": f"{method} /{path} is what {curated} does: call "
                f"{curated} instead, with the id from find_media",
            }
        literal = (
            f"{method} /{path}"
            + (f" ({tag})" if tag else "")
            + (f" ?{json.dumps(params)}" if params else "")
            + (f" {json.dumps(body)}" if body is not None else "")
        )
        scope = (
            service,
            method,
            path,
            tag,
            json.dumps(params, sort_keys=True),
            json.dumps(body, sort_keys=True),
        )
        asked = ctx.asked()
        turn = ctx.turn()
        # `fields` shapes only what comes back, so it is not part of what a
        # user confirms, but it does make a different read.
        read = scope + (json.dumps(sorted(keys)),)
        if answered["turn"] != turn:
            answered.update(turn=turn, reads={})
        if method == "GET" and turn is not None and read in answered["reads"]:
            log.warn("tool_refused", tool=f"{service}_api", reason="repeat", path=path)
            repeat = {
                "ok": False,
                "error": "this exact request already ran this turn and its "
                "answer is above; asking again returns the same answer",
            }
            if answered["reads"][read]:
                repeat["detail"] = (
                    "that answer was cut: call again with `fields` set to only "
                    "the keys you need"
                )
            return {"service": service, "request": literal, **repeat}

        def run():
            """Send, and answer with one receipt: ok is whether the service
            did what was asked, never whether the wire worked. Every attempt
            is a tool_gap with its outcome, so the failures that show what
            the curated tools lack are counted alongside the successes."""
            status = None
            try:
                result = send(method, path, params, body)
            except HttpFailure as e:
                status = e.status
                body_, truncated = _cap(scrub(e.body))
                out = {"ok": False, "error": f"{service} {e.reason}", "result": body_}
            except Exception as e:
                # Through the scrub: a transport error can quote the URL.
                err = scrub(str(e))
                m = HTTP_STATUS_RE.search(err)
                status = int(m.group(1)) if m else None
                log.error("tool_error", tool=f"{service}_api", err=err)
                out = {"ok": False, "error": err}
                if method != "GET" and status is None:
                    out["detail"] = (
                        "the request may or may not have reached the service - "
                        "read its state before calling again"
                    )
            else:
                status = 200
                result = scrub(result)
                missed = None
                if keys:
                    picked = _select(result, keys)
                    if picked is _DROP:
                        missed, picked = _key_names(result), {}
                    result = picked
                size = len(json.dumps(result, default=str))
                capped, truncated = _cap(result)
                out = {"ok": True, "result": capped}
                if missed is not None:
                    out["detail"] = (
                        "none of `fields` is in the response; the keys it has "
                        "are listed in `keys`"
                    )
                    out["keys"] = missed[:MAX_KEYS_SHOWN]
                if truncated:
                    # Most of these APIs cannot page, so the way to less is
                    # naming the keys, listed here so they need no guessing.
                    out["truncated"] = True
                    out["detail"] = (
                        f"cut at {MAX_RESULT_CHARS} of {size} characters: call "
                        "again with `fields` set to only the keys you need"
                        + (" - fewer than this time" if keys else "")
                    )
                    out["keys"] = _key_names(result)[:MAX_KEYS_SHOWN]
                if method == "GET" and turn is not None:
                    answered["reads"][read] = truncated
            # `api`, not `service`: that name belongs to the log record itself.
            log(
                "tool_gap",
                api=service,
                method=method,
                path=path,
                ok=out["ok"],
                status=status,
                asked=str(asked)[:120],
            )
            if method != "GET":
                _record(service, method, path, literal, out)
            return {"service": service, "request": literal, **out}

        if method == "GET":
            return run()
        # A yes only runs the request it was asked about, byte for byte. When
        # the model rebuilds the body for the same endpoint, say so, or the
        # user's yes is spent on a fresh question nobody can see.
        note = ""
        if any(s[:4] == scope[:4] and s != scope for s in ctx.gate.pending()):
            note = (
                f"a different {method} /{path} is already waiting for a yes. If "
                "the user's yes was for that one, call again with that request "
                "exactly as first sent; this one is a new question"
            )
        # The text lane shows the literal, body included; the voice lane has
        # the model read it back in words.
        return Plan(scope, "", run, f"run {literal}", confirm=literal, note=note)

    def _record(service, method, path, literal, out):
        """Record a passthrough write in the ledger as finished, so `operations
        list` shows it."""
        if operations is None:
            return
        try:
            row = operations.track_external(
                "api_write",
                service,
                literal,
                f"{service} {method} /{path}",
                turn=ctx.turn(),
            )
            failure = " - ".join(
                str(s) for s in (out.get("error"), out.get("detail")) if s
            )
            operations.observe(
                row["id"],
                operations_mod.SUCCEEDED if out["ok"] else operations_mod.FAILED,
                {},
                "" if out["ok"] else failure,
                summary=f"{service} {method} /{path} "
                + ("ran." if out["ok"] else "failed."),
                announce=False,
            )
        except Exception as e:
            log.error("tool_error", tool=f"{service}_api", err=f"ledger: {e}")

    def _arr(client):
        def send(method, path, params, body):
            return client.call(method, path, params=params or None, payload=body)

        return send

    def _qbit(method, path, params, body):
        # qBittorrent takes form fields, not JSON. A read carries its params
        # in the query; an action posts them as form fields.
        if method == "GET":
            return media.qbit.call("GET", path, params=params or None)
        return media.qbit.call(
            method, path, params=None, payload={**params, **(body or {})}
        )

    def _steam(method, path, params, body, auth):
        import requests

        host, _, rest = path.partition("/")
        if host in STEAM_HOSTS:
            url = f"https://{host}/{rest}"
        else:
            url = f"https://api.steampowered.com/{path}"
            host = "api.steampowered.com"
        params = dict(params)
        if auth == "account":
            if steam is None or not steam.available():
                raise RuntimeError("the Steam account session is not enrolled")
            params["access_token"] = steam.access_token()
        elif auth == "key" and host == "api.steampowered.com":
            creds = library.steam_creds()
            if not creds:
                raise RuntimeError("no steamApiKey in secrets")
            params["key"] = creds[0]
            params.setdefault("steamid", creds[1])
        if not url.endswith("/") and host == "api.steampowered.com":
            url += "/"
        data: Any = None
        headers = {"Accept": "application/json"}
        if method != "GET" and body is not None:
            # Steam's Web API takes flat fields as form data; a nested body
            # (a Service method's lists and messages) has to travel as one
            # input_json field. An array body is nested by definition.
            nested = isinstance(body, list) or any(
                isinstance(v, (dict, list)) for v in body.values()
            )
            if host == "api.steampowered.com" and nested and "input_json" not in body:
                data = {"input_json": json.dumps(body)}
            elif nested:
                # The store and community sites take flat fields as a form
                # (sessionid-style endpoints) and anything nested as a JSON
                # document: a form cannot carry a list or a nested object.
                data = json.dumps(body)
                headers["Content-Type"] = "application/json"
            else:
                data = body
        try:
            r = requests.request(
                method,
                url,
                params=params,
                data=data,
                timeout=20,
                headers=headers,
            )
        except requests.RequestException as e:
            # Never the message: it quotes the URL, credential and all.
            raise RuntimeError(f"steam request failed ({type(e).__name__})") from None
        try:
            value = r.json()
        except ValueError:
            value = r.text[:MAX_RESULT_CHARS]
        envelope = {
            "status": r.status_code,
            "eresult": r.headers.get("X-eresult"),
            "body": value,
        }
        if r.status_code >= 400:
            raise HttpFailure(r.status_code, envelope)
        if session.refused(envelope["eresult"]):
            # A 200 with a failing X-eresult is Steam saying no, the same
            # way the session's own calls read it.
            raise HttpFailure(
                r.status_code, envelope, f"refused (code {envelope['eresult']})"
            )
        return envelope

    @bind
    def describe_api(args):
        service = str(args.get("service") or "")
        topic = str(args.get("topic") or "")
        if service not in apidocs.SOURCES:
            return {"ok": False, "error": f"unknown service {service}"}
        base = (media.cfg or {}).get(f"{service}Url") if media is not None else None
        try:
            return {"ok": True, **apidocs.describe(service, topic, base)}
        except Exception as e:
            log.error("tool_error", tool="describe_api", err=str(e))
            return {"ok": False, "error": str(e)}

    @bind.destructive
    def radarr_api(args):
        return _run("radarr", args, _arr(media.radarr))

    @bind.destructive
    def sonarr_api(args):
        return _run("sonarr", args, _arr(media.sonarr))

    @bind.destructive
    def prowlarr_api(args):
        return _run("prowlarr", args, _arr(media.prowlarr))

    @bind.destructive
    def qbittorrent_api(args):
        return _run("qbittorrent", args, _qbit)

    @bind.destructive
    def steam_api(args):
        auth = str(args.get("auth") or "none")
        if auth not in ("none", "key", "account"):
            return {"ok": False, "error": "auth must be none, key or account"}
        return _run(
            "steam",
            args,
            lambda m, p, q, b: _steam(m, p, q, b, auth),
            tag=f"auth: {auth}",
        )

    return bind.impls()
