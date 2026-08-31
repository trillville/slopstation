"""Authenticated transports for the media sidecars, and the names every media
module shares.

Nothing above this module speaks HTTP. The shared vocabulary - the errors,
KINDS, _kind, _clean_text, _root_and_profile_gaps, _qbit_from_config - lives
here because its callers span media.py, media_proton.py and media_checks.py.
This is the leaf: it imports no sibling.
"""

import http.cookies
import json
import urllib.error
import urllib.parse
import urllib.request

# Every local sidecar is on the LAN; a slow answer is a broken one.
HTTP_TIMEOUT_S = 10


class MediaError(RuntimeError):
    pass


class MediaConfigurationError(MediaError):
    pass


class QbittorrentAuthError(MediaError):
    pass


def _clean_text(value, limit=160):
    return "".join(c for c in str(value or "").strip()
                   if c.isprintable())[:limit]


"""`authority` is the lowercase name the operation ledger and the
download-client category use; ArrClient.name is the capitalized one the API
errors carry."""
KINDS = {
    "movie": {"client": "radarr", "authority": "radarr", "resource": "movie",
              "id_key": "tmdbId", "public_key": "tmdb_id",
              "root_key": "movieRoot", "presets_key": "moviePresets",
              "category_field": "movieCategory"},
    "series": {"client": "sonarr", "authority": "sonarr", "resource": "series",
               "id_key": "tvdbId", "public_key": "tvdb_id",
               "root_key": "seriesRoot", "presets_key": "seriesPresets",
               "category_field": "tvCategory"},
}


def _kind(kind):
    try:
        return KINDS[kind]
    except KeyError:
        raise MediaError(f"unknown media kind {kind}") from None


def _root_and_profile_gaps(root_paths, profile_names, wanted_root, wanted_profiles):
    """The two library-policy answers validate() and the doctor both need.
    Pure: the caller fetches and names what it wants, so each keeps its own
    behaviour on missing config. Roots compare case-folded and without a
    trailing separator - Servarr echoes the path back in either form."""
    normalized = {str(path).rstrip("/\\").casefold() for path in root_paths}
    root_exists = (bool(wanted_root)
                   and str(wanted_root).rstrip("/\\").casefold() in normalized)
    available = {str(name).casefold() for name in profile_names}
    missing = [name for name in wanted_profiles
               if str(name).casefold() not in available]
    return root_exists, missing


def _http_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        path = urllib.parse.urlsplit(url).path
        raise MediaError(f"media service returned HTTP {e.code} for {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MediaError("media service is unreachable") from e
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise MediaError("media service returned malformed JSON") from e


class ArrClient:
    """Small authenticated JSON client for one local Servarr API."""

    def __init__(self, name, base_url, api_key, api_version="v3",
                 transport=None):
        parsed = urllib.parse.urlsplit(str(base_url).rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MediaConfigurationError(f"{name} URL is invalid")
        self.name = name
        self.base_url = str(base_url).rstrip("/")
        self.api_version = api_version
        self.api_key = api_key
        self.transport = transport or _http_transport
        self.timeout = HTTP_TIMEOUT_S

    def request(self, method, endpoint, params=None, payload=None):
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/api/{self.api_version}/{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "X-Api-Key": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.transport(method, url, headers, body, self.timeout)

    def get(self, endpoint, params=None):
        return self.request("GET", endpoint, params=params)

    def post(self, endpoint, payload):
        return self.request("POST", endpoint, payload=payload)

    def put(self, endpoint, payload):
        return self.request("PUT", endpoint, payload=payload)

    def delete(self, endpoint, params=None):
        return self.request("DELETE", endpoint, params=params)


def _qbit_http_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as e:
        path = urllib.parse.urlsplit(url).path
        error_type = QbittorrentAuthError if e.code in (401, 403) else MediaError
        raise error_type(f"qBittorrent returned HTTP {e.code} for {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MediaError("qBittorrent is unreachable") from e


class QbittorrentClient:
    """Authenticated boundary for diagnostics and explicit maintenance."""

    def __init__(self, base_url, username, password, transport=None):
        parsed = urllib.parse.urlsplit(str(base_url).rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MediaConfigurationError("qBittorrent URL is invalid")
        if not isinstance(username, str) or not username:
            raise MediaConfigurationError("media.qbittorrentUsername is missing")
        if not isinstance(password, str) or not password:
            raise MediaConfigurationError("qbittorrentPassword is missing")
        self.base_url = str(base_url).rstrip("/")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.username = username
        self.password = password
        self.transport = transport or _qbit_http_transport
        self.timeout = HTTP_TIMEOUT_S
        self.sid = None
        self.sid_cookie = None

    def _call(self, method, endpoint, payload=None, authenticate=True):
        if authenticate and self.sid is None:
            self.login()

        def send():
            body = None
            headers = {
                "Accept": "application/json",
                "Origin": self.origin,
                "Referer": self.base_url + "/",
            }
            if payload is not None:
                body = urllib.parse.urlencode(payload).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            if self.sid is not None and self.sid_cookie is not None:
                headers["Cookie"] = f"{self.sid_cookie}={self.sid}"
            return self.transport(
                method, f"{self.base_url}/api/v2/{endpoint.lstrip('/')}",
                headers, body, self.timeout)

        try:
            return send()
        except QbittorrentAuthError:
            if not authenticate:
                raise
            self.sid = None
            self.sid_cookie = None
            self.login()
            return send()

    def login(self):
        headers, raw = self._call("POST", "auth/login", {
            "username": self.username,
            "password": self.password,
        }, authenticate=False)
        if raw.decode("utf-8", "replace").strip() not in ("", "Ok."):
            raise MediaError("qBittorrent rejected the configured credentials")
        cookie = http.cookies.SimpleCookie()
        for key, value in headers.items():
            if str(key).casefold() == "set-cookie":
                cookie.load(value)
        for name in cookie:
            if name in ("QBT_SID", "SID") or name.startswith("QBT_SID_"):
                self.sid_cookie = name
                self.sid = cookie[name].value
                break
        if self.sid is None:
            raise MediaError("qBittorrent login returned no session cookie")

    def _text(self, endpoint):
        _, raw = self._call("GET", endpoint)
        return raw.decode("utf-8", "replace").strip()

    def _json(self, endpoint):
        _, raw = self._call("GET", endpoint)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise MediaError("qBittorrent returned malformed JSON") from e
        return value

    def version(self):
        return self._text("app/version")

    def preferences(self):
        value = self._json("app/preferences")
        if not isinstance(value, dict):
            raise MediaError("qBittorrent returned invalid preferences")
        return value

    def categories(self):
        value = self._json("torrents/categories")
        if not isinstance(value, dict):
            raise MediaError("qBittorrent returned invalid categories")
        return value

    def set_preferences(self, changes):
        self._call("POST", "app/setPreferences", {
            "json": json.dumps(changes, separators=(",", ":")),
        })

    def set_listen_port(self, port):
        try:
            port = int(port)
        except (TypeError, ValueError) as e:
            raise MediaError("listening port must be an integer") from e
        if not 1 <= port <= 65535:
            raise MediaError("listening port must be between 1 and 65535")
        before = self.preferences()
        previous = int(before.get("listen_port", 0) or 0)
        if previous != port:
            self.set_preferences({"listen_port": port})
        after = self.preferences()
        confirmed = int(after.get("listen_port", 0) or 0)
        if confirmed != port:
            raise MediaError(
                f"qBittorrent did not retain listening port {port}")
        return {"ok": True, "previous_port": previous,
                "listen_port": confirmed, "changed": previous != confirmed}


def _configured_password(value):
    return (isinstance(value, str) and "..." not in value
            and not value.upper().startswith("PLACEHOLDER")
            and len(value.strip()) >= 6)


def _qbit_from_config(media_cfg, secrets, transport=None):
    password = secrets.get("qbittorrentPassword")
    if not _configured_password(password):
        raise MediaConfigurationError("qbittorrentPassword is missing")
    return QbittorrentClient(
        media_cfg.get("qbittorrentUrl", ""),
        media_cfg.get("qbittorrentUsername", ""), password,
        transport=transport)
