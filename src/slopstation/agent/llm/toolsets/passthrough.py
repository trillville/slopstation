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

from slopstation.agent.llm.registry import ToolContext, ToolSpec
from slopstation.agent.tools import apidocs, library

METHODS = ("GET", "POST", "PUT", "DELETE")
PATH_RE = re.compile(r"^[A-Za-z0-9/_.\-{}]{1,200}$")
MAX_RESULT_CHARS = 8000

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|password|passwd|secret|passkey|authorization|cookie)",
    re.I,
)
# Private trackers put the account passkey in the announce URL.
TRACKER_RE = re.compile(r"^(udp|https?)://\S*(announce|passkey|authkey)", re.I)

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
ARR_BLOCKED_READ = ("config/host", "system/backup", "config/downloadclient")
QBIT_BLOCKED = (
    "app/shutdown",
    "app/setPreferences",
    "app/setCookies",
    "rss/",
    "search/installPlugin",
    "search/uninstallPlugin",
    "search/updatePlugins",
    "torrents/removeCategories",
    "torrents/deleteTags",
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
            "type": "object",
            "description": "request body (JSON object; form fields for qBittorrent)",
            "additionalProperties": True,
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
    """Redact secret-shaped fields and tracker URLs, recursively."""
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if SECRET_KEY_RE.search(str(k)) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and TRACKER_RE.match(value):
        return "[redacted tracker url]"
    return value


def _cap(result):
    text = json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result, False
    return text[:MAX_RESULT_CHARS] + " ...", True


def _blocked(service, method, path):
    p = path.lower()
    if service in ("radarr", "sonarr", "prowlarr"):
        if method != "GET" and p.startswith(ARR_BLOCKED_WRITE):
            return True
        if p.startswith(ARR_BLOCKED_READ):
            return True
    if service == "qbittorrent":
        return any(p.startswith(b.lower()) for b in QBIT_BLOCKED)
    return False


def impls(ctx: ToolContext):
    dispatch, log, media, steam = ctx.dispatch, ctx.log, ctx.media, ctx.steam

    def _run(service, args, send):
        method = str(args.get("method") or "GET").upper()
        path = str(args.get("path") or "").strip().lstrip("/")
        params = args.get("params") or {}
        body = args.get("body")
        if method not in METHODS:
            return {"ok": False, "error": f"method must be one of {', '.join(METHODS)}"}
        if not path or not PATH_RE.match(path) or ".." in path.split("/"):
            return {"ok": False, "error": "path must be a plain API path"}
        if not isinstance(params, dict) or (
            body is not None and not isinstance(body, dict)
        ):
            return {"ok": False, "error": "params and body must be objects"}
        if _blocked(service, method, path):
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
            + (f" ?{json.dumps(params)}" if params else "")
            + (f" {json.dumps(body)}" if body is not None else "")
        )
        if method != "GET":
            if dispatch.dry_run:
                log("dry_run_would", action=f"{service}: {literal}")
                return {"ok": True, "dry_run": True, "detail": f"would run {literal}"}
            scope = (
                service,
                method,
                path,
                json.dumps(params, sort_keys=True),
                json.dumps(body, sort_keys=True),
            )
            if not ctx.gate.confirmed(scope, dispatch.utterance.turn):
                log.warn(
                    "tool_refused",
                    tool=f"{service}_api",
                    reason="unconfirmed",
                    path=path,
                )
                return {
                    "ok": False,
                    "confirm": literal,
                    "error": "not run yet: read this request back to the user in "
                    "plain words, and call again unchanged once they say yes",
                }
        try:
            result = send(method, path, params, body)
        except Exception as e:
            log.error("tool_error", tool=f"{service}_api", err=str(e))
            return {"ok": False, "error": str(e)}
        result, truncated = _cap(scrub(result))
        asked = getattr(dispatch.utterance, "asked", None) or ""
        # `api`, not `service`: that name belongs to the log record itself.
        log(
            "tool_gap",
            api=service,
            method=method,
            path=path,
            asked=str(asked)[:120],
        )
        out = {"ok": True, "service": service, "request": literal, "result": result}
        if truncated:
            out["truncated"] = True
            out["detail"] = "the response was cut; ask for less or page it"
        return out

    def _arr(client):
        def send(method, path, params, body):
            return client.call(method, path, params=params or None, payload=body)

        return send

    def _qbit(method, path, params, body):
        # qBittorrent takes form fields, not JSON; a GET carries them as query.
        if method == "GET":
            return media.qbit.call("GET", path, params={**params, **(body or {})})
        return media.qbit.call(method, path, params=params or None, payload=body or {})

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
        r = requests.request(
            method,
            url,
            params=params,
            data=body if method != "GET" else None,
            timeout=20,
            headers={"Accept": "application/json"},
        )
        try:
            value = r.json()
        except ValueError:
            value = r.text[:MAX_RESULT_CHARS]
        return {
            "status": r.status_code,
            "eresult": r.headers.get("X-eresult"),
            "body": value,
        }

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

    def radarr_api(args):
        return _run("radarr", args, _arr(media.radarr))

    def sonarr_api(args):
        return _run("sonarr", args, _arr(media.sonarr))

    def prowlarr_api(args):
        return _run("prowlarr", args, _arr(media.prowlarr))

    def qbittorrent_api(args):
        return _run("qbittorrent", args, _qbit)

    def steam_api(args):
        auth = str(args.get("auth") or "none")
        if auth not in ("none", "key", "account"):
            return {"ok": False, "error": "auth must be none, key or account"}
        return _run("steam", args, lambda m, p, q, b: _steam(m, p, q, b, auth))

    return {
        "describe_api": describe_api,
        "radarr_api": radarr_api,
        "sonarr_api": sonarr_api,
        "prowlarr_api": prowlarr_api,
        "qbittorrent_api": qbittorrent_api,
        "steam_api": steam_api,
    }
