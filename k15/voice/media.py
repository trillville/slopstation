"""Radarr and Sonarr request/observation boundary.

Prowlarr and qBittorrent stay behind the two authorities. No release or
indexer result crosses this module's public interface.
"""
import argparse
import datetime
import http.cookies
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cglib

PRESETS = ("default", "1080p", "2160p")


class MediaError(RuntimeError):
    pass


class MediaConfigurationError(MediaError):
    pass


def _clean_text(value, limit=160):
    return "".join(c for c in str(value or "").strip()
                   if c.isprintable())[:limit]


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
                 transport=None, timeout=10):
        parsed = urllib.parse.urlsplit(str(base_url).rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MediaConfigurationError(f"{name} URL is invalid")
        self.name = name
        self.base_url = str(base_url).rstrip("/")
        self.api_version = api_version
        self.api_key = api_key
        self.transport = transport or _http_transport
        self.timeout = timeout

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

    def delete(self, endpoint, params=None, payload=None):
        return self.request("DELETE", endpoint, params=params, payload=payload)


def _qbit_http_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as e:
        path = urllib.parse.urlsplit(url).path
        raise MediaError(
            f"qBittorrent returned HTTP {e.code} for {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MediaError("qBittorrent is unreachable") from e


class QbittorrentClient:
    """Authenticated boundary for diagnostics and explicit maintenance."""

    def __init__(self, base_url, username, password, transport=None, timeout=10):
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
        self.timeout = timeout
        self.sid = None
        self.sid_cookie = None

    def _call(self, method, endpoint, payload=None, authenticate=True):
        if authenticate and self.sid is None:
            self.login()
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
        for name in ("QBT_SID", "SID"):
            if name in cookie:
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


class MediaService:
    """Resolve policy names and submit/observe concrete media requests."""

    def __init__(self, cfg, log, radarr, sonarr):
        self.cfg = cfg
        self.log = log
        self.radarr = radarr
        self.sonarr = sonarr

    def _client(self, kind):
        if kind == "movie":
            return self.radarr
        if kind == "series":
            return self.sonarr
        raise MediaError(f"unknown media kind {kind}")

    def find(self, kind, query):
        query = _clean_text(query)
        if not query:
            raise MediaError("media lookup needs a title")
        client = self._client(kind)
        endpoint = "movie/lookup" if kind == "movie" else "series/lookup"
        rows = client.get(endpoint, {"term": query})
        if not isinstance(rows, list):
            raise MediaError(f"{client.name} returned an invalid lookup")
        out = []
        id_key = "tmdbId" if kind == "movie" else "tvdbId"
        public_key = "tmdb_id" if kind == "movie" else "tvdb_id"
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                catalog_id = int(row.get(id_key, 0))
                year = int(row.get("year", 0))
            except (TypeError, ValueError):
                continue
            title = _clean_text(row.get("title"))
            if catalog_id <= 0 or not title:
                continue
            out.append({public_key: catalog_id, "title": title, "year": year,
                        "status": _clean_text(row.get("status"), 40)})
            if len(out) == 5:
                break
        return out

    def profiles(self):
        return {
            "movie": self._profile_names(self.radarr),
            "series": self._profile_names(self.sonarr),
        }

    def validate(self):
        """Check configured roots and preset targets without mutating either service."""
        status = self.status()
        profiles = self.profiles()
        checks = {}
        for kind, client, root_key, presets_key in (
                ("movie", self.radarr, "movieRoot", "moviePresets"),
                ("series", self.sonarr, "seriesRoot", "seriesPresets")):
            rows = client.get("rootfolder")
            if not isinstance(rows, list):
                raise MediaError(f"{client.name} returned invalid root folders")
            roots = [_clean_text(row.get("path")) for row in rows
                     if isinstance(row, dict) and row.get("path")]
            wanted_root = str(self.cfg[root_key])
            normalized = {path.rstrip("/\\").casefold() for path in roots}
            root_exists = wanted_root.rstrip("/\\").casefold() in normalized
            available = {name.casefold() for name in profiles[kind]}
            wanted_profiles = sorted(set(self.cfg[presets_key].values()))
            missing_profiles = [name for name in wanted_profiles
                                if str(name).casefold() not in available]
            checks[kind] = {
                "configured_root": wanted_root,
                "root_exists": root_exists,
                "missing_profiles": missing_profiles,
            }
        return {
            "ok": all(check["root_exists"] and not check["missing_profiles"]
                      for check in checks.values()),
            "services": status,
            "checks": checks,
        }

    @staticmethod
    def _profile_names(client):
        rows = client.get("qualityprofile")
        if not isinstance(rows, list):
            raise MediaError(f"{client.name} returned invalid quality profiles")
        return [_clean_text(r.get("name")) for r in rows
                if isinstance(r, dict) and r.get("name")]

    def _profile(self, kind, preset):
        preset = str(preset or "default").lower()
        key = "moviePresets" if kind == "movie" else "seriesPresets"
        mapping = self.cfg.get(key, {})
        if preset not in mapping:
            allowed = ", ".join(sorted(mapping)) or "none"
            raise MediaConfigurationError(
                f"unknown {kind} preset {preset}; configured presets: {allowed}")
        wanted = str(mapping[preset])
        client = self._client(kind)
        rows = client.get("qualityprofile")
        if not isinstance(rows, list):
            raise MediaError(f"{client.name} returned invalid quality profiles")
        for row in rows:
            if (isinstance(row, dict)
                    and str(row.get("name", "")).casefold() == wanted.casefold()):
                try:
                    return int(row["id"]), wanted
                except (KeyError, TypeError, ValueError) as e:
                    raise MediaError(f"{client.name} profile has no id") from e
        raise MediaConfigurationError(
            f"{client.name} has no quality profile named {wanted}")

    @staticmethod
    def _one(value, authority, resource):
        if not isinstance(value, dict):
            raise MediaError(f"{authority} returned an invalid {resource}")
        return value

    @staticmethod
    def _existing(value, catalog_key, catalog_id, authority):
        if not isinstance(value, list):
            raise MediaError(f"{authority} returned an invalid library response")
        for row in value:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get(catalog_key, 0) or 0) == catalog_id:
                    return row
            except (TypeError, ValueError):
                continue
        return None

    def request_movie(self, tmdb_id, preset="default"):
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError) as e:
            raise MediaError("tmdb_id must be an integer") from e
        if tmdb_id <= 0:
            raise MediaError("tmdb_id must be positive")
        profile_id, profile_name = self._profile("movie", preset)
        existing = self._existing(
            self.radarr.get("movie", {"tmdbId": tmdb_id}),
            "tmdbId", tmdb_id, "Radarr")

        if existing is not None:
            movie = dict(existing)
            movie_id = int(movie["id"])
            title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
            try:
                current_profile_id = int(movie.get("qualityProfileId", 0))
            except (TypeError, ValueError):
                current_profile_id = 0
            if movie.get("hasFile") and current_profile_id == profile_id:
                return self._submission("movie", movie_id, title, tmdb_id,
                                        preset, profile_name, True)
            baseline_file_id = None
            if movie.get("hasFile"):
                rows = self.radarr.get("moviefile", {"movieId": movie_id})
                if not isinstance(rows, list) or not rows:
                    raise MediaError("Radarr reports a movie file but did not return it")
                try:
                    baseline_file_id = int(rows[0]["id"])
                except (KeyError, TypeError, ValueError) as e:
                    raise MediaError("Radarr movie file has no id") from e
            movie.update(qualityProfileId=profile_id, monitored=True)
            self.radarr.put(f"movie/{movie_id}", movie)
            command = self._one(
                self.radarr.post("command", {"name": "MoviesSearch",
                                              "movieIds": [movie_id]}),
                "Radarr", "search command")
            command_ids = [int(command["id"])]
        else:
            candidate = self._one(
                self.radarr.get("movie/lookup/tmdb", {"tmdbId": tmdb_id}),
                "Radarr", "movie lookup")
            payload = dict(candidate)
            payload.pop("id", None)
            payload.update(
                rootFolderPath=self.cfg["movieRoot"],
                qualityProfileId=profile_id,
                monitored=True,
                minimumAvailability="released",
                addOptions={"searchForMovie": False, "addMethod": "manual"},
            )
            movie = self._one(self.radarr.post("movie", payload),
                              "Radarr", "created movie")
            movie_id = int(movie["id"])
            title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
            baseline_file_id = None
            command = self._one(
                self.radarr.post("command", {"name": "MoviesSearch",
                                              "movieIds": [movie_id]}),
                "Radarr", "search command")
            command_ids = [int(command["id"])]
        return self._submission("movie", movie_id, title, tmdb_id, preset,
                                profile_name, False,
                                baseline_file_id=baseline_file_id,
                                command_ids=command_ids)

    @staticmethod
    def _seasons(value):
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise MediaError("seasons must be a non-empty list or omitted")
        try:
            seasons = sorted({int(n) for n in value})
        except (TypeError, ValueError) as e:
            raise MediaError("season numbers must be integers") from e
        if any(n <= 0 for n in seasons):
            raise MediaError("season numbers must be positive; specials are explicit")
        return seasons

    def _set_series_seasons(self, series, selected):
        out = dict(series)
        seasons = []
        for season in out.get("seasons") or []:
            if not isinstance(season, dict):
                continue
            row = dict(season)
            number = int(row.get("seasonNumber", -1))
            if number > 0 and (selected is None or number in selected):
                row["monitored"] = True
            seasons.append(row)
        out.update(qualityProfileId=out["qualityProfileId"], monitored=True,
                   seasons=seasons)
        return out

    def _search_series(self, series_id, seasons):
        if seasons is None:
            command = self._one(
                self.sonarr.post("command", {"name": "SeriesSearch",
                                              "seriesId": series_id}),
                "Sonarr", "search command")
            return [int(command["id"])]
        command_ids = []
        for season in seasons:
            command = self._one(
                self.sonarr.post("command", {"name": "SeasonSearch",
                                              "seriesId": series_id,
                                              "seasonNumber": season}),
                "Sonarr", "search command")
            command_ids.append(int(command["id"]))
        return command_ids

    @staticmethod
    def _episode_metadata_ready(rows, seasons):
        if not isinstance(rows, list) or not rows:
            return False
        wanted = set(seasons or [])
        available = {int(row.get("seasonNumber", 0) or 0) for row in rows
                     if isinstance(row, dict)}
        return bool(available - {0}) if not wanted else wanted <= available

    def _monitor_series_episodes(self, rows, seasons):
        wanted = set(seasons or [])
        episode_ids = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            season = int(row.get("seasonNumber", 0) or 0)
            if season <= 0 or (wanted and season not in wanted):
                continue
            if row.get("monitored"):
                continue
            try:
                episode_id = int(row["id"])
            except (KeyError, TypeError, ValueError) as e:
                raise MediaError("Sonarr episode has no id") from e
            if episode_id <= 0:
                raise MediaError("Sonarr episode has no id")
            episode_ids.append(episode_id)
        if episode_ids:
            self.sonarr.put("episode/monitor", {
                "episodeIds": episode_ids,
                "monitored": True,
            })

    def dispatch_pending_series_search(self, operation):
        metadata = operation.get("metadata") or {}
        if (operation.get("kind") != "series_acquisition"
                or not metadata.get("search_pending")):
            return False
        series_id = int(operation["external_ref"])
        seasons = self._seasons(metadata.get("seasons")) \
            if metadata.get("seasons") is not None else None
        rows = self.sonarr.get("episode", {"seriesId": series_id})
        if not self._episode_metadata_ready(rows, seasons):
            return False
        self._monitor_series_episodes(rows, seasons)
        return self._search_series(series_id, seasons)

    def request_series(self, tvdb_id, preset="default", seasons=None):
        try:
            tvdb_id = int(tvdb_id)
        except (TypeError, ValueError) as e:
            raise MediaError("tvdb_id must be an integer") from e
        if tvdb_id <= 0:
            raise MediaError("tvdb_id must be positive")
        seasons = self._seasons(seasons)
        profile_id, profile_name = self._profile("series", preset)
        existing = self._existing(
            self.sonarr.get("series", {"tvdbId": tvdb_id}),
            "tvdbId", tvdb_id, "Sonarr")
        search_pending = False
        command_ids = []

        if existing is not None:
            series = dict(existing)
            series_id = int(series["id"])
            title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
            try:
                profile_changed = int(series.get("qualityProfileId", 0)) != profile_id
            except (TypeError, ValueError):
                profile_changed = True
            baseline_episode_files = None
            if profile_changed:
                rows = self.sonarr.get("episode", {"seriesId": series_id})
                targets = self._target_episodes(rows, seasons,
                                                monitored_only=False)
                baseline_episode_files = {}
                for episode in targets:
                    if not episode.get("hasFile"):
                        continue
                    try:
                        episode_id = int(episode["id"])
                        file_id = int(episode["episodeFileId"])
                    except (KeyError, TypeError, ValueError) as e:
                        raise MediaError("Sonarr episode file has no id") from e
                    baseline_episode_files[str(episode_id)] = file_id
            series["qualityProfileId"] = profile_id
            series = self._set_series_seasons(series, seasons)
            self.sonarr.put(f"series/{series_id}", series)
            observation = self.observe_series(
                series_id, seasons,
                baseline_episode_files=baseline_episode_files)
            if observation["complete"]:
                return self._submission("series", series_id, title, tvdb_id,
                                        preset, profile_name, True, seasons)
            if observation["metadata_ready"]:
                command_ids = self._search_series(series_id, seasons)
            else:
                search_pending = True
        else:
            rows = self.sonarr.get("series/lookup", {"term": f"tvdb:{tvdb_id}"})
            candidate = self._existing(rows, "tvdbId", tvdb_id, "Sonarr")
            if candidate is None:
                raise MediaError(f"Sonarr could not resolve TVDB {tvdb_id}")
            payload = dict(candidate)
            payload.pop("id", None)
            payload.update(
                rootFolderPath=self.cfg["seriesRoot"],
                qualityProfileId=profile_id,
                seasonFolder=True,
                monitored=True,
                addOptions={
                    "monitor": "all" if seasons is None else "none",
                    "searchForMissingEpisodes": False,
                    "searchForCutoffUnmetEpisodes": False,
                },
            )
            series = self._one(self.sonarr.post("series", payload),
                               "Sonarr", "created series")
            series_id = int(series["id"])
            title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
            baseline_episode_files = None
            if seasons is not None:
                series["qualityProfileId"] = profile_id
                series = self._set_series_seasons(series, seasons)
                self.sonarr.put(f"series/{series_id}", series)
            search_pending = True
        return self._submission("series", series_id, title, tvdb_id, preset,
                                profile_name, False, seasons,
                                baseline_episode_files=baseline_episode_files,
                                search_pending=search_pending,
                                command_ids=command_ids)

    @staticmethod
    def _submission(kind, external_ref, title, catalog_id, preset, profile,
                    already_available, seasons=None, baseline_file_id=None,
                    baseline_episode_files=None, search_pending=False,
                    command_ids=None):
        out = {
            "ok": True,
            "kind": f"{kind}_acquisition",
            "authority": "radarr" if kind == "movie" else "sonarr",
            "external_ref": str(external_ref),
            "title": title,
            "catalog_id": catalog_id,
            "preset": str(preset or "default").lower(),
            "profile": profile,
            "already_available": already_available,
        }
        if kind == "series":
            out["seasons"] = seasons
        if baseline_file_id is not None:
            out["baseline_file_id"] = baseline_file_id
        if baseline_episode_files is not None:
            out["baseline_episode_files"] = baseline_episode_files
        if search_pending:
            out["search_pending"] = True
        if command_ids:
            out["command_ids"] = command_ids
        return out

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(
                str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _command_phase(client, command_ids):
        statuses = []
        for command_id in command_ids or []:
            try:
                row = client.get(f"command/{int(command_id)}")
            except MediaError as e:
                if "HTTP 404" in str(e):
                    continue
                raise
            if not isinstance(row, dict):
                continue
            status = str(row.get("status", "")).lower()
            result = str(row.get("result", "")).lower()
            if status in ("failed", "aborted", "cancelled", "orphaned"):
                raise MediaError(f"{client.name} search {status}")
            if status == "completed" and result == "unsuccessful":
                raise MediaError(f"{client.name} search failed")
            statuses.append(status)
        if any(status in ("queued", "started") for status in statuses):
            return "searching"
        return "waiting_for_match"

    @staticmethod
    def _queue_records(client, id_key, wanted_id):
        queue = client.get("queue", {"page": 1, "pageSize": 1000})
        if not isinstance(queue, dict):
            return []
        records = []
        for row in queue.get("records", []):
            if not isinstance(row, dict):
                continue
            try:
                matches = int(row.get(id_key, 0) or 0) == wanted_id
            except (TypeError, ValueError):
                matches = False
            if matches:
                records.append(row)
        return records

    @staticmethod
    def _queue_progress(records):
        downloads = {}
        for index, row in enumerate(records):
            key = str(row.get("downloadId") or f"row-{index}")
            current = downloads.get(key)
            size = float(row.get("size", 0) or 0)
            left = float(row.get("sizeleft", 0) or 0)
            if current is None or size > current[0]:
                downloads[key] = (size, left)
        size = sum(row[0] for row in downloads.values())
        left = sum(row[1] for row in downloads.values())
        if size <= 0:
            return None
        return max(0, min(100, round((size - left) * 100 / size)))

    def observe_movie(self, movie_id, baseline_file_id=None, command_ids=None,
                      previous_phase=None):
        movie = self._one(self.radarr.get(f"movie/{int(movie_id)}"),
                          "Radarr", "movie")
        if movie.get("hasFile"):
            if baseline_file_id is None:
                return {"complete": True,
                        "progress": {"phase": "ready", "percent": 100},
                        "detail": "Radarr reports the movie imported"}
            rows = self.radarr.get("moviefile", {"movieId": int(movie_id)})
            if not isinstance(rows, list) or not rows:
                raise MediaError("Radarr reports a movie file but did not return it")
            try:
                current_file_id = int(rows[0]["id"])
            except (KeyError, TypeError, ValueError) as e:
                raise MediaError("Radarr movie file has no id") from e
            if current_file_id != int(baseline_file_id):
                return {"complete": True,
                        "progress": {"phase": "ready", "percent": 100},
                        "detail": "Radarr imported the requested movie upgrade"}
        records = self._queue_records(self.radarr, "movieId", int(movie_id))
        percent = self._queue_progress(records)
        if records:
            progress = {"phase": "downloading"}
            if percent is not None:
                progress["percent"] = percent
            detail = (f"download is {percent}% complete" if percent is not None
                      else "the movie download is active")
        else:
            phase = ("importing" if previous_phase == "downloading" else
                     self._command_phase(self.radarr, command_ids))
            progress = {"phase": phase}
            detail = ("Radarr is importing the requested movie file"
                      if phase == "importing" else
                      "Radarr is searching for an acceptable movie release"
                      if phase == "searching" else
                      "no acceptable movie release is available yet; Radarr is watching")
        return {"complete": False, "progress": progress, "detail": detail}

    def _target_episodes(self, rows, seasons=None, now=None,
                         monitored_only=True):
        seasons = self._seasons(seasons) if seasons is not None else None
        if not isinstance(rows, list):
            raise MediaError("Sonarr returned invalid episodes")
        now = now or datetime.datetime.now(datetime.timezone.utc)
        targets = []
        for episode in rows:
            if not isinstance(episode, dict):
                continue
            number = int(episode.get("seasonNumber", 0) or 0)
            if number <= 0 or (seasons is not None and number not in seasons):
                continue
            if monitored_only and not episode.get("monitored"):
                continue
            aired = self._parse_time(episode.get("airDateUtc"))
            if aired is None or aired > now:
                continue
            targets.append(episode)
        return targets

    def observe_series(self, series_id, seasons=None, now=None,
                       baseline_episode_files=None, command_ids=None,
                       previous_phase=None):
        rows = self.sonarr.get("episode", {"seriesId": int(series_id)})
        metadata_ready = self._episode_metadata_ready(rows, seasons)
        scope = [row for row in rows if isinstance(row, dict)
                 and int(row.get("seasonNumber", 0) or 0) > 0
                 and (seasons is None
                      or int(row.get("seasonNumber", 0) or 0) in seasons)]
        if metadata_ready and scope and not any(
                row.get("monitored") for row in scope):
            return {
                "complete": False,
                "canceled": True,
                "progress": {"episodes": 0, "total_episodes": 0,
                             "percent": 0},
                "detail": "Sonarr reports the requested episodes are unmonitored",
                "metadata_ready": True}
        targets = self._target_episodes(rows, seasons, now)
        baseline = baseline_episode_files or {}
        total = len(targets)
        ready = 0
        for episode in targets:
            if not episode.get("hasFile"):
                continue
            old_file_id = baseline.get(str(episode.get("id")))
            if old_file_id is not None:
                try:
                    if int(episode.get("episodeFileId", 0)) == int(old_file_id):
                        continue
                except (TypeError, ValueError):
                    continue
            ready += 1
        percent = round(ready * 100 / total) if total else 0
        progress = {"episodes": ready, "total_episodes": total,
                    "percent": percent}
        if not metadata_ready:
            progress["phase"] = "searching"
            detail = "Sonarr is still populating episode metadata"
            complete = False
        elif not total:
            progress["phase"] = "waiting_for_match"
            detail = "no requested monitored episodes have aired yet"
            complete = False
        elif ready == total:
            progress["phase"] = "ready"
            detail = f"{ready} of {total} aired episodes are ready"
            complete = True
        else:
            queue = self._queue_records(self.sonarr, "seriesId", int(series_id))
            wanted_ids = {int(row["id"]) for row in targets if row.get("id")}
            queue = [row for row in queue
                     if not row.get("episodeId")
                     or int(row.get("episodeId", 0) or 0) in wanted_ids]
            queue_percent = self._queue_progress(queue)
            if queue:
                progress["phase"] = "downloading"
                if queue_percent is not None:
                    progress["download_percent"] = queue_percent
                detail = (f"download is {queue_percent}% complete; "
                          f"{ready} of {total} episodes are imported"
                          if queue_percent is not None else
                          f"download is active; {ready} of {total} episodes are imported")
            else:
                phase = ("importing" if previous_phase == "downloading" else
                         self._command_phase(self.sonarr, command_ids))
                progress["phase"] = phase
                detail = (f"Sonarr is importing episodes; {ready} of {total} are ready"
                          if phase == "importing" else
                          "Sonarr is searching for acceptable episode releases"
                          if phase == "searching" else
                          "no acceptable episode release is available yet; Sonarr is watching")
            complete = False
        return {"complete": complete, "progress": progress, "detail": detail,
                "metadata_ready": metadata_ready}

    def observe(self, operation, now=None):
        kind = operation.get("kind")
        external_ref = int(operation["external_ref"])
        if kind == "movie_acquisition":
            metadata = operation.get("metadata") or {}
            return self.observe_movie(external_ref,
                                      metadata.get("baseline_file_id"),
                                      metadata.get("command_ids"),
                                      (operation.get("progress") or {}).get("phase"))
        if kind == "series_acquisition":
            metadata = operation.get("metadata") or {}
            return self.observe_series(
                external_ref, metadata.get("seasons"), now,
                metadata.get("baseline_episode_files"),
                metadata.get("command_ids"),
                (operation.get("progress") or {}).get("phase"))
        raise MediaError(f"unsupported media operation kind {kind}")

    @staticmethod
    def _queue_delete_params():
        return {"removeFromClient": True, "blocklist": False,
                "skipRedownload": True, "changeCategory": False}

    @staticmethod
    def _cancel_commands(client, command_ids):
        for command_id in sorted({int(value) for value in command_ids or []}):
            try:
                row = client.get(f"command/{command_id}")
            except MediaError as e:
                if "HTTP 404" in str(e):
                    continue
                raise
            if (isinstance(row, dict)
                    and str(row.get("status", "")).lower() in ("queued", "started")):
                client.delete(f"command/{command_id}")

    def _remove_queue(self, client, records):
        seen = set()
        removed = 0
        for row in records:
            key = str(row.get("downloadId") or f"queue-{row.get('id')}")
            if key in seen:
                continue
            seen.add(key)
            client.delete(f"queue/{int(row['id'])}", self._queue_delete_params())
            removed += 1
        return removed

    def delete_movie(self, tmdb_id, command_ids=None):
        tmdb_id = int(tmdb_id)
        movie = self._existing(self.radarr.get("movie", {"tmdbId": tmdb_id}),
                               "tmdbId", tmdb_id, "Radarr")
        if movie is None:
            return {"ok": True, "kind": "movie", "catalog_id": tmdb_id,
                    "removed": False, "detail": "the movie was not managed by Radarr"}
        movie_id = int(movie["id"])
        title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
        unmonitored = dict(movie)
        unmonitored["monitored"] = False
        self.radarr.put(f"movie/{movie_id}", unmonitored)
        self._cancel_commands(self.radarr, command_ids)
        queued = self._queue_records(self.radarr, "movieId", movie_id)
        downloads = self._remove_queue(self.radarr, queued)
        had_file = bool(movie.get("hasFile"))
        self.radarr.delete(f"movie/{movie_id}", {
            "deleteFiles": True, "addImportExclusion": False})
        return {"ok": True, "kind": "movie", "catalog_id": tmdb_id,
                "title": title, "removed": True, "downloads_canceled": downloads,
                "files_deleted": 1 if had_file else 0,
                "detail": f"removed {title} from Radarr and deleted its files"}

    def delete_series(self, tvdb_id, seasons=None, all_seasons=False,
                      command_ids=None):
        tvdb_id = int(tvdb_id)
        selected = self._seasons(seasons) if seasons is not None else None
        if selected is None and not all_seasons:
            raise MediaError("series deletion needs seasons or explicit all_seasons")
        series = self._existing(self.sonarr.get("series", {"tvdbId": tvdb_id}),
                                "tvdbId", tvdb_id, "Sonarr")
        if series is None:
            return {"ok": True, "kind": "series", "catalog_id": tvdb_id,
                    "removed": False, "detail": "the series was not managed by Sonarr"}
        series_id = int(series["id"])
        title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
        self._cancel_commands(self.sonarr, command_ids)
        if all_seasons:
            queued = self._queue_records(self.sonarr, "seriesId", series_id)
            downloads = self._remove_queue(self.sonarr, queued)
            self.sonarr.delete(f"series/{series_id}", {
                "deleteFiles": True, "addImportListExclusion": False})
            return {"ok": True, "kind": "series", "catalog_id": tvdb_id,
                    "title": title, "removed": True,
                    "downloads_canceled": downloads, "all_seasons": True,
                    "detail": f"removed all seasons of {title} from Sonarr and deleted their files"}

        episodes = self.sonarr.get("episode", {"seriesId": series_id})
        if not isinstance(episodes, list):
            raise MediaError("Sonarr returned invalid episodes")
        wanted = [row for row in episodes if isinstance(row, dict)
                  and int(row.get("seasonNumber", 0) or 0) in selected]
        episode_ids = sorted({int(row["id"]) for row in wanted if row.get("id")})
        if episode_ids:
            self.sonarr.put("episode/monitor", {
                "episodeIds": episode_ids, "monitored": False})
        updated = dict(series)
        updated["seasons"] = [
            {**row, "monitored": False}
            if isinstance(row, dict)
            and int(row.get("seasonNumber", -1)) in selected else row
            for row in series.get("seasons") or []]
        self.sonarr.put(f"series/{series_id}", updated)
        wanted_ids = set(episode_ids)
        queued = [row for row in self._queue_records(
            self.sonarr, "seriesId", series_id)
            if int(row.get("episodeId", 0) or 0) in wanted_ids]
        downloads = self._remove_queue(self.sonarr, queued)
        file_ids = sorted({int(row.get("episodeFileId", 0) or 0)
                           for row in wanted if row.get("hasFile")
                           and int(row.get("episodeFileId", 0) or 0) > 0})
        for file_id in file_ids:
            self.sonarr.delete(f"episodefile/{file_id}")
        season_text = ", ".join(str(n) for n in selected)
        return {"ok": True, "kind": "series", "catalog_id": tvdb_id,
                "title": title, "removed": True, "seasons": selected,
                "downloads_canceled": downloads, "files_deleted": len(file_ids),
                "detail": f"deleted season {season_text} of {title} and stopped monitoring it"}

    def status(self):
        out = {}
        for name, client in (("radarr", self.radarr), ("sonarr", self.sonarr)):
            row = client.get("system/status")
            row = self._one(row, client.name, "system status")
            out[name] = {"version": _clean_text(row.get("version"), 40),
                         "app": _clean_text(row.get("appName"), 40)}
        return out


class DoctorReport:
    def __init__(self):
        self.checks = []

    def add(self, level, name, detail):
        self.checks.append({"level": level, "name": name,
                            "detail": _clean_text(detail, 240)})

    def result(self):
        return {"ok": not any(row["level"] == "FAIL" for row in self.checks),
                "checks": list(self.checks)}


def _compose_services(media_dir):
    env_file = media_dir / ".env"
    if not env_file.is_file():
        raise MediaError(f"Compose environment file is missing: {env_file}")
    command = [
        "docker", "compose", "--project-directory", str(media_dir),
        "--env-file", str(env_file), "ps", "--format", "json",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=20, check=False)
    except (FileNotFoundError, OSError) as e:
        raise MediaError("Docker CLI is unavailable") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError("Docker Compose status timed out") from e
    if completed.returncode:
        detail = _clean_text(completed.stderr) or "Docker Compose status failed"
        raise MediaError(detail)
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = [rows]
    except ValueError:
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError as e:
            raise MediaError("Docker Compose returned malformed status") from e
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise MediaError("Docker Compose returned invalid status")
    return rows


def _row_field(row, *names):
    wanted = {name.casefold() for name in names}
    for field in row.get("fields") or []:
        if (isinstance(field, dict)
                and str(field.get("name", "")).casefold() in wanted):
            return field.get("value")
    return None


def _number_matches(value, expected):
    try:
        return float(value) == float(expected)
    except (TypeError, ValueError):
        return False


def _enabled_rows(rows):
    return [row for row in rows if isinstance(row, dict)
            and row.get("enable", row.get("enabled", True))]


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


def _check_arr(report, kind, client, media_cfg):
    label = client.name
    try:
        status = client.get("system/status")
        if not isinstance(status, dict):
            raise MediaError(f"{label} returned invalid status")
        report.add("PASS", f"{label} API",
                   f"reachable, version {_clean_text(status.get('version'), 40)}")
    except MediaError as e:
        report.add("FAIL", f"{label} API", str(e))
        return

    try:
        health = client.get("health")
        if not isinstance(health, list):
            raise MediaError(f"{label} returned invalid health status")
        if health:
            sources = sorted({_clean_text(row.get("source"), 40) for row in health
                              if isinstance(row, dict) and row.get("source")})
            detail = f"{len(health)} warning(s)"
            if sources:
                detail += ": " + ", ".join(sources[:5])
            report.add("WARN", f"{label} health", detail)
        else:
            report.add("PASS", f"{label} health", "no health warnings")
    except MediaError as e:
        report.add("FAIL", f"{label} health", str(e))

    root_key = "movieRoot" if kind == "movie" else "seriesRoot"
    presets_key = "moviePresets" if kind == "movie" else "seriesPresets"
    try:
        roots = client.get("rootfolder")
        profiles = client.get("qualityprofile")
        if not isinstance(roots, list) or not isinstance(profiles, list):
            raise MediaError(f"{label} returned invalid roots or profiles")
        wanted_root = str(media_cfg.get(root_key, ""))
        root_names = {str(row.get("path", "")).rstrip("/\\").casefold()
                      for row in roots if isinstance(row, dict)}
        if wanted_root and wanted_root.rstrip("/\\").casefold() in root_names:
            report.add("PASS", f"{label} root", wanted_root)
        else:
            report.add("FAIL", f"{label} root",
                       f"configured root {wanted_root or '(missing)'} does not exist")
        available = {str(row.get("name", "")).casefold() for row in profiles
                     if isinstance(row, dict)}
        wanted = sorted(set((media_cfg.get(presets_key) or {}).values()))
        missing = [name for name in wanted if str(name).casefold() not in available]
        if missing:
            report.add("FAIL", f"{label} quality profiles",
                       "missing: " + ", ".join(missing))
        else:
            report.add("PASS", f"{label} quality profiles",
                       f"all {len(wanted)} configured profile(s) exist")
    except MediaError as e:
        report.add("FAIL", f"{label} library policy", str(e))

    try:
        indexers = client.get("indexer")
        if not isinstance(indexers, list):
            raise MediaError(f"{label} returned invalid indexers")
        enabled = _enabled_rows(indexers)
        if enabled:
            report.add("PASS", f"{label} indexers",
                       f"{len(enabled)} enabled indexer(s)")
        else:
            report.add("FAIL", f"{label} indexers", "no enabled indexers")
    except MediaError as e:
        report.add("FAIL", f"{label} indexers", str(e))

    try:
        clients = client.get("downloadclient")
        if not isinstance(clients, list):
            raise MediaError(f"{label} returned invalid download clients")
        qbittorrent = [row for row in _enabled_rows(clients)
                       if str(row.get("implementation", "")).casefold()
                       == "qbittorrent"]
        expected_category = "radarr" if kind == "movie" else "sonarr"
        if not qbittorrent:
            report.add("FAIL", f"{label} qBittorrent client",
                       "no enabled qBittorrent download client")
        else:
            category_field = "movieCategory" if kind == "movie" else "tvCategory"
            categories = {_clean_text(
                _row_field(row, category_field, "category"), 80).casefold()
                          for row in qbittorrent}
            if expected_category in categories:
                report.add("PASS", f"{label} qBittorrent client",
                           f"enabled with {expected_category} category")
            else:
                report.add("FAIL", f"{label} qBittorrent client",
                           f"expected category {expected_category}")
        completed = client.get("config/downloadclient")
        if not isinstance(completed, dict):
            raise MediaError(f"{label} returned invalid download handling")
        handling = bool(completed.get("enableCompletedDownloadHandling"))
        removal = bool(qbittorrent) and all(
            row.get("removeCompletedDownloads") for row in qbittorrent)
        if not handling:
            report.add("FAIL", f"{label} completed-download handling",
                       "completed-download handling is disabled")
        elif handling and removal:
            report.add("PASS", f"{label} completed-download removal",
                       "enabled after import and seed-goal completion")
        elif handling and qbittorrent:
            report.add("WARN", f"{label} completed-download removal",
                       "handling is enabled, but Remove Completed Downloads is disabled on qBittorrent")
    except MediaError as e:
        report.add("FAIL", f"{label} download client", str(e))


def _check_prowlarr(report, client, media_cfg):
    try:
        status = client.get("system/status")
        if not isinstance(status, dict):
            raise MediaError("Prowlarr returned invalid status")
        report.add("PASS", "Prowlarr API",
                   f"reachable, version {_clean_text(status.get('version'), 40)}")
    except MediaError as e:
        report.add("FAIL", "Prowlarr API", str(e))
        return
    try:
        health = client.get("health")
        if not isinstance(health, list):
            raise MediaError("Prowlarr returned invalid health status")
        report.add("WARN" if health else "PASS", "Prowlarr health",
                   f"{len(health)} warning(s)" if health else "no health warnings")
    except MediaError as e:
        report.add("FAIL", "Prowlarr health", str(e))

    try:
        rows = client.get("indexer")
        if not isinstance(rows, list):
            raise MediaError("Prowlarr returned invalid indexers")
        expected_names = media_cfg.get("managedIndexers") or []
        ratio = media_cfg.get("seedRatio")
        minutes = media_cfg.get("seedTimeMinutes")
        by_name = {str(row.get("name", "")).casefold(): row
                   for row in rows if isinstance(row, dict)}
        for name in expected_names:
            row = by_name.get(str(name).casefold())
            if row is None or row not in _enabled_rows([row]):
                report.add("FAIL", f"Prowlarr indexer {name}", "missing or disabled")
                continue
            actual_ratio = _row_field(
                row, "torrentBaseSettings.seedRatio", "seedRatio")
            actual_time = _row_field(
                row, "torrentBaseSettings.seedTime", "seedTime")
            if (_number_matches(actual_ratio, ratio)
                    and _number_matches(actual_time, minutes)):
                report.add("PASS", f"Prowlarr indexer {name}",
                           f"ratio {ratio}, seed time {minutes} minutes")
            else:
                report.add("FAIL", f"Prowlarr indexer {name}",
                           f"expected ratio {ratio} and seed time {minutes} minutes")
        if not expected_names:
            report.add("WARN", "Prowlarr managed indexers",
                       "media.managedIndexers is empty")
    except MediaError as e:
        report.add("FAIL", "Prowlarr indexers", str(e))

    try:
        rows = client.get("applications")
        if not isinstance(rows, list):
            raise MediaError("Prowlarr returned invalid applications")
        for wanted in ("radarr", "sonarr"):
            matches = [row for row in rows if isinstance(row, dict)
                       and wanted in (str(row.get("implementation", "")) + " "
                                      + str(row.get("name", ""))).casefold()]
            if not matches:
                report.add("FAIL", f"Prowlarr {wanted} sync", "application is missing")
            elif any("full" in str(row.get("syncLevel", "")).casefold()
                     for row in matches):
                report.add("PASS", f"Prowlarr {wanted} sync", "Full Sync")
            else:
                report.add("FAIL", f"Prowlarr {wanted} sync", "Full Sync is not enabled")
    except MediaError as e:
        report.add("FAIL", "Prowlarr applications", str(e))


def _check_qbittorrent(report, client, media_cfg):
    try:
        version = client.version()
        preferences = client.preferences()
        categories = client.categories()
    except MediaError as e:
        report.add("FAIL", "qBittorrent API", str(e))
        return
    report.add("PASS", "qBittorrent API", f"reachable, version {version}")
    expected_interface = str(media_cfg.get(
        "qbittorrentNetworkInterface", "ProtonVPN"))
    interfaces = [str(preferences.get(key, "")) for key in
                  ("current_network_interface", "current_interface_name")]
    if any(value.casefold() == expected_interface.casefold() for value in interfaces):
        report.add("PASS", "qBittorrent interface", expected_interface)
    else:
        actual = next((value for value in interfaces if value), "All interfaces")
        report.add("FAIL", "qBittorrent interface",
                   f"expected {expected_interface}; found {actual}")
    address = str(preferences.get("current_interface_address", ""))
    report.add("PASS" if not address else "WARN", "qBittorrent optional IP",
               "All addresses" if not address else f"restricted to {address}")
    report.add("FAIL" if preferences.get("upnp") else "PASS",
               "qBittorrent UPnP/NAT-PMP",
               "enabled" if preferences.get("upnp") else "disabled")
    try:
        port = int(preferences.get("listen_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    report.add("PASS" if 1 <= port <= 65535 else "FAIL",
               "qBittorrent listening port", str(port or "invalid"))
    action = preferences.get("max_ratio_act")
    report.add("PASS" if action == 0 else "FAIL", "qBittorrent share-limit action",
               "Stop" if action == 0 else "must be Stop, never Remove")
    mode = str(preferences.get("share_limits_mode", ""))
    report.add("PASS" if mode.casefold() == "matchany" else "FAIL",
               "qBittorrent share-limit mode",
               mode or "must be MatchAny (either limit)")
    auth_bypass = (preferences.get("bypass_local_auth")
                   or preferences.get("bypass_auth_subnet_whitelist_enabled"))
    report.add("FAIL" if auth_bypass else "PASS", "qBittorrent Web UI auth",
               "authentication bypass is enabled" if auth_bypass
               else "no localhost or subnet bypass")
    category_names = {str(name).casefold() for name in categories}
    missing = [name for name in ("radarr", "sonarr")
               if name not in category_names]
    report.add("FAIL" if missing else "PASS", "qBittorrent categories",
               "missing: " + ", ".join(missing) if missing
               else "radarr and sonarr are present")


def media_doctor(cfg, secrets, log, arr_transport=None, qbit_transport=None,
                 compose_runner=None):
    """Read live configuration without changing any service."""
    report = DoctorReport()
    media_cfg = cfg.get("media") if isinstance(cfg, dict) else None
    if not isinstance(media_cfg, dict):
        report.add("FAIL", "Slopstation media config", "media section is missing")
        return report.result()
    report.add("PASS" if media_cfg.get("enabled") else "FAIL",
               "Slopstation media config",
               "enabled" if media_cfg.get("enabled") else "media.enabled is false")

    media_dir = Path(__file__).resolve().parent.parent / "media"
    try:
        rows = (compose_runner or _compose_services)(media_dir)
        states = {str(row.get("Service", row.get("service", ""))).casefold(): row
                  for row in rows}
        bad = []
        for name in ("flaresolverr", "prowlarr", "radarr", "sonarr"):
            row = states.get(name)
            state = str((row or {}).get("State", (row or {}).get("state", "")))
            health = str((row or {}).get("Health", (row or {}).get("health", "")))
            if (row is None or state.casefold() != "running"
                    or health.casefold() not in ("", "healthy")):
                bad.append(name)
        report.add("FAIL" if bad else "PASS", "Docker media containers",
                   "not ready: " + ", ".join(bad) if bad
                   else "FlareSolverr, Prowlarr, Radarr, and Sonarr are running")
    except MediaError as e:
        report.add("FAIL", "Docker media containers", str(e))

    clients = {}
    for name, key, url_key in (("Radarr", "radarrApiKey", "radarrUrl"),
                               ("Sonarr", "sonarrApiKey", "sonarrUrl")):
        if not cglib.real_key(secrets.get(key)):
            report.add("FAIL", f"{name} API", f"{key} is missing")
            continue
        try:
            clients[name] = ArrClient(name, media_cfg.get(url_key, ""),
                                      secrets[key], transport=arr_transport)
        except MediaConfigurationError as e:
            report.add("FAIL", f"{name} API", str(e))
    if "Radarr" in clients:
        _check_arr(report, "movie", clients["Radarr"], media_cfg)
    if "Sonarr" in clients:
        _check_arr(report, "series", clients["Sonarr"], media_cfg)

    if cglib.real_key(secrets.get("prowlarrApiKey")):
        try:
            prowlarr = ArrClient(
                "Prowlarr", media_cfg.get("prowlarrUrl", ""),
                secrets["prowlarrApiKey"], api_version="v1",
                transport=arr_transport)
            _check_prowlarr(report, prowlarr, media_cfg)
        except MediaConfigurationError as e:
            report.add("FAIL", "Prowlarr API", str(e))
    else:
        report.add("FAIL", "Prowlarr API", "prowlarrApiKey is missing")

    try:
        _check_qbittorrent(
            report, _qbit_from_config(media_cfg, secrets, qbit_transport),
            media_cfg)
    except MediaConfigurationError as e:
        report.add("FAIL", "qBittorrent API", str(e))
    report.add("WARN", "Proton assigned port",
               "Windows Proton VPN does not expose it through a supported API; compare Active port manually")
    report.add("WARN", "Torrent-visible VPN address",
               "not tested; run an explicit torrent address test after VPN changes")
    return report.result()


def from_config(cfg, secrets, log, transport=None):
    media_cfg = cfg.get("media") if isinstance(cfg, dict) else None
    if not isinstance(media_cfg, dict) or not media_cfg.get("enabled"):
        return None
    missing = [name for name in ("radarrApiKey", "sonarrApiKey")
               if not cglib.real_key(secrets.get(name))]
    if missing:
        log.warn("lane_disabled", what="media",
                 reason="missing media API keys: " + ", ".join(missing))
        return None
    try:
        radarr = ArrClient("Radarr", media_cfg["radarrUrl"],
                           secrets["radarrApiKey"], transport=transport)
        sonarr = ArrClient("Sonarr", media_cfg["sonarrUrl"],
                           secrets["sonarrApiKey"], transport=transport)
        for key in ("movieRoot", "seriesRoot"):
            if not isinstance(media_cfg.get(key), str) or not media_cfg[key]:
                raise MediaConfigurationError(f"media.{key} is missing")
        for key in ("moviePresets", "seriesPresets"):
            mapping = media_cfg.get(key)
            if (not isinstance(mapping, dict) or not mapping
                    or not all(isinstance(name, str) and name
                               for name in mapping.values())):
                raise MediaConfigurationError(f"media.{key} is invalid")
        poll_s = media_cfg.get("pollS", 30)
        if not isinstance(poll_s, (int, float)) or poll_s <= 0:
            raise MediaConfigurationError("media.pollS must be positive")
    except (KeyError, MediaConfigurationError) as e:
        log.warn("lane_disabled", what="media", reason=str(e))
        return None
    return MediaService(media_cfg, log, radarr, sonarr)


def _track(store, submission):
    if submission["already_available"]:
        return submission
    metadata = {k: submission.get(k) for k in
                ("catalog_id", "preset", "profile", "seasons",
                 "baseline_file_id", "baseline_episode_files",
                 "search_pending", "command_ids")
                if k in submission}
    try:
        operation = store.track_external(
            submission["kind"], submission["authority"],
            submission["external_ref"], submission["title"],
            detail=f"{submission['authority'].title()} accepted the request",
            metadata=metadata)
        operation = store.observe(
            operation["id"], "RUNNING", {"phase": "searching"},
            f"{submission['authority'].title()} accepted the request and is searching")
        return {**submission, "operation_id": operation["id"],
                "phase": "searching"}
    except Exception as e:
        # Submission already happened; a failed local write must not invite a
        # second external request from the diagnostic CLI.
        store.log.error("tool_error", tool="track_media_cli", err=str(e))
        return {**submission, "tracking": "failed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect and request media")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("profiles")
    sub.add_parser("validate")
    sub.add_parser("doctor")
    qbit_port = sub.add_parser("set-qbit-port")
    qbit_port.add_argument("port", type=int)
    qbit_port.add_argument("--execute", action="store_true")
    find = sub.add_parser("find")
    find.add_argument("kind", choices=("movie", "series"))
    find.add_argument("query")
    movie = sub.add_parser("request-movie")
    movie.add_argument("tmdb_id", type=int)
    movie.add_argument("--preset", choices=PRESETS, default="default")
    movie.add_argument("--execute", action="store_true")
    series = sub.add_parser("request-series")
    series.add_argument("tvdb_id", type=int)
    series.add_argument("--preset", choices=PRESETS, default="default")
    series.add_argument("--season", action="append", type=int, dest="seasons")
    series.add_argument("--execute", action="store_true")
    delete_movie = sub.add_parser("delete-movie")
    delete_movie.add_argument("tmdb_id", type=int)
    delete_movie.add_argument("--execute", action="store_true")
    delete_series = sub.add_parser("delete-series")
    delete_series.add_argument("tvdb_id", type=int)
    scope = delete_series.add_mutually_exclusive_group(required=True)
    scope.add_argument("--season", action="append", type=int, dest="seasons")
    scope.add_argument("--all-seasons", action="store_true")
    delete_series.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    log = cglib.make_log("voice")
    cfg = cglib.config()
    secrets = cglib.load_secrets()
    try:
        if args.command == "doctor":
            result = media_doctor(cfg, secrets, log)
            for check in result["checks"]:
                print(f"{check['level']:<5} {check['name']} - {check['detail']}")
            return 0 if result["ok"] else 1
        if args.command == "set-qbit-port":
            if not args.execute:
                print("change not submitted; repeat with --execute")
                return 2
            media_cfg = cfg.get("media") if isinstance(cfg, dict) else None
            if not isinstance(media_cfg, dict):
                raise MediaConfigurationError("media configuration is missing")
            result = _qbit_from_config(media_cfg, secrets).set_listen_port(args.port)
            print(json.dumps(result, indent=2))
            return 0

        service = from_config(cfg, secrets, log)
        if service is None:
            print("media is disabled or its configuration/API keys are incomplete")
            return 1
        if args.command == "status":
            result = service.status()
        elif args.command == "profiles":
            result = service.profiles()
        elif args.command == "validate":
            result = service.validate()
        elif args.command == "find":
            result = service.find(args.kind, args.query)
        elif not args.execute:
            print("change not submitted; repeat with --execute")
            return 2
        else:
            import operations
            store = operations.OperationStore(log)
            if args.command == "request-movie":
                result = _track(store, service.request_movie(
                    args.tmdb_id, args.preset))
            elif args.command == "request-series":
                result = _track(store, service.request_series(
                    args.tvdb_id, args.preset, args.seasons))
            elif args.command == "delete-movie":
                active = [row for row in store.active(kind="movie_acquisition")
                          if int((row.get("metadata") or {}).get(
                              "catalog_id", 0) or 0) == args.tmdb_id]
                command_ids = [command_id for row in active
                               for command_id in (row.get("metadata") or {}).get(
                                   "command_ids", [])]
                result = service.delete_movie(args.tmdb_id, command_ids)
                for row in active:
                    store.observe(row["id"], operations.CANCELED,
                                  row.get("progress", {}),
                                  "the media request was deleted cleanly")
                    store.mark_delivered(row["id"])
            else:
                active = []
                for row in store.active(kind="series_acquisition"):
                    metadata = row.get("metadata") or {}
                    if int(metadata.get("catalog_id", 0) or 0) != args.tvdb_id:
                        continue
                    requested = metadata.get("seasons")
                    if args.all_seasons or (requested is not None
                                            and set(requested) <= set(args.seasons or [])):
                        active.append(row)
                command_ids = [command_id for row in active
                               for command_id in (row.get("metadata") or {}).get(
                                   "command_ids", [])]
                result = service.delete_series(
                    args.tvdb_id, args.seasons, args.all_seasons, command_ids)
                for row in active:
                    store.observe(row["id"], operations.CANCELED,
                                  row.get("progress", {}),
                                  "the media request was deleted cleanly")
                    store.mark_delivered(row["id"])
        print(json.dumps(result, indent=2))
        if args.command == "validate" and not result["ok"]:
            return 1
        return 0
    except MediaError as e:
        print(f"media request failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
