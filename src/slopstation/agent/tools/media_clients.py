"""Authenticated HTTP clients and shared types for media services."""

import datetime
import http.cookies
import json
import urllib.error
import urllib.parse
import urllib.request

# Every local sidecar is on the LAN; a slow answer is a broken one.
HTTP_TIMEOUT_S = 10
# Except the interactive release search, which fans out to every indexer
# over the internet and answers when the slowest one does.
SEARCH_TIMEOUT_S = 90


class MediaError(RuntimeError):
    pass


class MediaConfigurationError(MediaError):
    pass


class QbittorrentAuthError(MediaError):
    pass


def _clean_text(value, limit=160):
    return "".join(c for c in str(value or "").strip() if c.isprintable())[:limit]


def _parse_time(value):
    """An ISO 8601 timestamp as a datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


# `authority` is the lowercase name the operation ledger, the download-client
# category and MediaService's client attribute use; ArrClient.name is the
# capitalized one the API errors carry.
KINDS = {
    "movie": {
        "authority": "radarr",
        "resource": "movie",
        "id_key": "tmdbId",
        "public_key": "tmdb_id",
        "root_key": "movieRoot",
        "presets_key": "moviePresets",
        "category_field": "movieCategory",
    },
    "series": {
        "authority": "sonarr",
        "resource": "series",
        "id_key": "tvdbId",
        "public_key": "tvdb_id",
        "root_key": "seriesRoot",
        "presets_key": "seriesPresets",
        "category_field": "tvCategory",
    },
}


def _kind(kind):
    try:
        return KINDS[kind]
    except KeyError:
        raise MediaError(f"unknown media kind {kind}") from None


def _split_url(name, value):
    parsed = urllib.parse.urlsplit(str(value).rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MediaConfigurationError(f"{name} URL is invalid")
    return parsed


def _http_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
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

    def __init__(self, name, base_url, api_key, api_version="v3", transport=None):
        self.name = name
        self.base_url = _split_url(name, base_url).geturl()
        self.api_version = api_version
        self.api_key = api_key
        self.transport = transport or _http_transport

    def request(self, method, endpoint, params=None, payload=None, timeout=None):
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/api/{self.api_version}/{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "X-Api-Key": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.transport(method, url, headers, body, timeout or HTTP_TIMEOUT_S)

    def get(self, endpoint, params=None, timeout=None):
        return self.request("GET", endpoint, params=params, timeout=timeout)

    def post(self, endpoint, payload):
        return self.request("POST", endpoint, payload=payload)

    def put(self, endpoint, payload):
        return self.request("PUT", endpoint, payload=payload)

    def delete(self, endpoint, params=None):
        return self.request("DELETE", endpoint, params=params)

    def call(self, method, endpoint, params=None, payload=None):
        """The passthrough shape: any method, any endpoint under the API root."""
        return self.request(method, endpoint, params=params, payload=payload)


def _qbit_http_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as e:
        path = urllib.parse.urlsplit(url).path
        error_type = QbittorrentAuthError if e.code in (401, 403) else MediaError
        raise error_type(f"qBittorrent returned HTTP {e.code} for {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MediaError("qBittorrent is unreachable") from e


def _configured_password(value):
    # events.real_key with the floor a Web UI password can meet.
    return (
        isinstance(value, str)
        and "..." not in value
        and not value.upper().startswith("PLACEHOLDER")
        and len(value.strip()) >= 6
    )


class QbittorrentClient:
    """Authenticated boundary for diagnostics and explicit maintenance."""

    def __init__(self, base_url, username, password, transport=None):
        parsed = _split_url("qBittorrent", base_url)
        if not isinstance(username, str) or not username:
            raise MediaConfigurationError("media.qbittorrentUsername is missing")
        if not _configured_password(password):
            raise MediaConfigurationError("qbittorrentPassword is missing")
        self.base_url = parsed.geturl()
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.username = username
        self.password = password
        self.transport = transport or _qbit_http_transport
        self.sid = None
        self.sid_cookie = None

    def _call(self, method, endpoint, payload=None, authenticate=True, params=None):
        if authenticate and self.sid is None:
            self.login()
        url = f"{self.base_url}/api/v2/{endpoint.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

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
            return self.transport(method, url, headers, body, HTTP_TIMEOUT_S)

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
        headers, raw = self._call(
            "POST",
            "auth/login",
            {
                "username": self.username,
                "password": self.password,
            },
            authenticate=False,
        )
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

    def _text(self, endpoint, params=None):
        _, raw = self._call("GET", endpoint, params=params)
        return raw.decode("utf-8", "replace").strip()

    def _json(self, endpoint, params=None):
        _, raw = self._call("GET", endpoint, params=params)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise MediaError("qBittorrent returned malformed JSON") from e
        return value

    def call(self, method, endpoint, params=None, payload=None):
        """The passthrough shape: JSON when the body parses, else the text,
        else None for an empty 200. qBittorrent takes form-encoded POSTs."""
        _, raw = self._call(method, endpoint, payload=payload, params=params)
        text = raw.decode("utf-8", "replace").strip() if raw else ""
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    # -- torrents (API v2.11, qBittorrent 5) -----------------------------------

    @staticmethod
    def _hashes(hashes):
        if hashes == "all":
            return "all"
        return "|".join(hashes)

    def torrents(
        self,
        filter=None,
        category=None,
        sort=None,
        reverse=False,
        limit=None,
        hashes=None,
    ):
        params = {}
        if hashes:
            params["hashes"] = self._hashes(hashes)
        if filter:
            params["filter"] = filter
        if category is not None:
            params["category"] = category
        if sort:
            params["sort"] = sort
            params["reverse"] = "true" if reverse else "false"
        if limit:
            params["limit"] = int(limit)
        value = self._json("torrents/info", params or None)
        if not isinstance(value, list):
            raise MediaError("qBittorrent returned an invalid torrent list")
        return value

    def torrent_properties(self, torrent_hash):
        return self._json("torrents/properties", {"hash": torrent_hash})

    def torrent_files(self, torrent_hash):
        return self._json("torrents/files", {"hash": torrent_hash})

    def torrent_trackers(self, torrent_hash):
        return self._json("torrents/trackers", {"hash": torrent_hash})

    def torrent_action(self, action, hashes):
        """stop, start, recheck, reannounce, topPrio, bottomPrio, increasePrio,
        decreasePrio - the ones that take only hashes."""
        self._call("POST", f"torrents/{action}", {"hashes": self._hashes(hashes)})

    def set_force_start(self, hashes, value):
        self._call(
            "POST",
            "torrents/setForceStart",
            {"hashes": self._hashes(hashes), "value": "true" if value else "false"},
        )

    def delete_torrents(self, hashes, delete_files):
        self._call(
            "POST",
            "torrents/delete",
            {
                "hashes": self._hashes(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )

    def set_torrent_limits(self, hashes, download=None, upload=None):
        """Per-torrent speed limits in bytes/s; 0 lifts one."""
        if download is not None:
            self._call(
                "POST",
                "torrents/setDownloadLimit",
                {"hashes": self._hashes(hashes), "limit": int(download)},
            )
        if upload is not None:
            self._call(
                "POST",
                "torrents/setUploadLimit",
                {"hashes": self._hashes(hashes), "limit": int(upload)},
            )

    # -- transfer ------------------------------------------------------------

    def transfer_info(self):
        value = self._json("transfer/info")
        if not isinstance(value, dict):
            raise MediaError("qBittorrent returned invalid transfer info")
        return value

    def speed_limits_mode(self):
        """True when the alternative limits are active."""
        return self._text("transfer/speedLimitsMode") == "1"

    def toggle_speed_limits_mode(self):
        self._call("POST", "transfer/toggleSpeedLimitsMode")

    def set_global_limits(self, download=None, upload=None):
        """Global speed limits in bytes/s; 0 lifts one."""
        if download is not None:
            self._call("POST", "transfer/setDownloadLimit", {"limit": int(download)})
        if upload is not None:
            self._call("POST", "transfer/setUploadLimit", {"limit": int(upload)})

    def main_log(self, warnings_only=True, last_known_id=-1):
        params = {
            "normal": "false" if warnings_only else "true",
            "info": "false" if warnings_only else "true",
            "warning": "true",
            "critical": "true",
            "last_known_id": int(last_known_id),
        }
        value = self._json("log/main", params)
        return value if isinstance(value, list) else []

    def server_state(self):
        """sync/maindata's server_state: free space on the download disk,
        connection status, DHT nodes, alternative-limits flag."""
        value = self._json("sync/maindata")
        state = value.get("server_state") if isinstance(value, dict) else None
        return state if isinstance(state, dict) else {}

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
        self._call(
            "POST",
            "app/setPreferences",
            {
                "json": json.dumps(changes, separators=(",", ":")),
            },
        )

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
            raise MediaError(f"qBittorrent did not retain listening port {port}")
        return {
            "ok": True,
            "previous_port": previous,
            "listen_port": confirmed,
            "changed": previous != confirmed,
        }

    def network_interfaces(self):
        value = self._json("app/networkInterfaceList")
        if not isinstance(value, list):
            raise MediaError("qBittorrent returned an invalid interface list")
        return value

    def rebind_interface(self, name):
        """Bind the peer sockets to the adapter called `name` as it exists
        now. A recreated adapter keeps its name and changes its id, so the
        stored id can point at nothing while the name still matches. The
        same id written twice is no change, so an unchanged one is cleared
        first to make libtorrent reopen the sockets."""
        wanted = str(name).casefold()
        row = next(
            (
                r
                for r in self.network_interfaces()
                if isinstance(r, dict) and str(r.get("name", "")).casefold() == wanted
            ),
            None,
        )
        if row is None:
            raise MediaError(f"qBittorrent has no network interface named {name}")
        value = str(row.get("value", ""))
        previous = str(self.preferences().get("current_network_interface", ""))
        if previous == value:
            self.set_preferences({"current_network_interface": ""})
        self.set_preferences({"current_network_interface": value})
        return {
            "interface": str(name),
            "previous": previous,
            "drifted": previous != value,
        }

    def shutdown(self):
        self._call("POST", "app/shutdown")


def _qbit_from_config(media_cfg, secrets):
    return QbittorrentClient(
        media_cfg.get("qbittorrentUrl", ""),
        media_cfg.get("qbittorrentUsername", ""),
        secrets.get("qbittorrentPassword"),
    )
