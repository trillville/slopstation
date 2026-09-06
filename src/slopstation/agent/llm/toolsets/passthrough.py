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

from slopstation.agent.llm.registry import Bindings, ToolContext, ToolSpec
from slopstation.agent.tools import apidocs, library, steam_session

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
    + _RESEARCH
)
SONARR_API = (
    "Any Sonarr v3 call: method, path under /api/v3, query params, JSON body. "
    + _RESEARCH
)
PROWLARR_API = (
    "Any Prowlarr v1 call: method, path under /api/v1, query params, JSON body. "
    + _RESEARCH
)
QBITTORRENT_API = (
    "Any qBittorrent v2 call on the authenticated session: method, path under "
    "/api/v2 (e.g. torrents/info, transfer/info), query params for GET, form "
    "fields as `body` for POST. " + _RESEARCH
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
    }
    props.update(extra or {})
    return props


def _spec(name, description, needs, keywords, extra=None):
    return ToolSpec(
        name,
        description,
        _params_schema(extra),
        ("method", "path"),
        risk="act",
        area="api",
        keywords=keywords,
        default=False,
        needs=needs,
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
    ),
    _spec(
        "radarr_api",
        RADARR_API,
        ("media",),
        ("radarr api", "raw radarr call", "movies api", "direct call"),
    ),
    _spec(
        "sonarr_api",
        SONARR_API,
        ("media",),
        ("sonarr api", "raw sonarr call", "series api", "direct call"),
    ),
    _spec(
        "prowlarr_api",
        PROWLARR_API,
        ("prowlarr",),
        ("prowlarr api", "indexers api", "raw prowlarr call", "direct call"),
    ),
    _spec(
        "qbittorrent_api",
        QBITTORRENT_API,
        ("torrents",),
        ("qbittorrent api", "raw torrent call", "qbit", "direct call"),
    ),
    _spec(
        "steam_api",
        STEAM_API,
        ("steam_data",),
        ("steam api", "steam web api", "raw steam call", "direct call"),
        extra={"auth": {"type": "string", "enum": ["none", "key", "account"]}},
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
    dispatch, log, media, steam = ctx.dispatch, ctx.log, ctx.media, ctx.steam

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
        asked = getattr(dispatch.utterance, "asked", None) or ""

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
                result, truncated = _cap(scrub(result))
                out = {"ok": True, "result": result}
                if truncated:
                    out["truncated"] = True
                    out["detail"] = "the response was cut; ask for less or page it"
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
            return {"service": service, "request": literal, **out}

        if method == "GET":
            return run()
        if dry := ctx.preview(f"run {literal}"):
            return dry
        # A refused or unanswered request keeps the ask armed: the model can
        # try again without asking the user twice.
        return ctx.confirm(
            f"{service}_api",
            scope,
            {
                "confirm": literal,
                "error": "not run yet: read this request back to the user in "
                "plain words, and call again unchanged once they say yes",
            },
            run,
        )

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
            elif isinstance(body, list):
                # The store and community sites take a JSON document, never a
                # form; requests cannot encode a list as one anyway.
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
        if steam_session.refused(envelope["eresult"]):
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

    @bind
    def radarr_api(args):
        return _run("radarr", args, _arr(media.radarr))

    @bind
    def sonarr_api(args):
        return _run("sonarr", args, _arr(media.sonarr))

    @bind
    def prowlarr_api(args):
        return _run("prowlarr", args, _arr(media.prowlarr))

    @bind
    def qbittorrent_api(args):
        return _run("qbittorrent", args, _qbit)

    @bind
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
