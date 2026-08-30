"""Radarr and Sonarr request/observation boundary.

Prowlarr and qBittorrent stay behind the two authorities. No release or
indexer result crosses this module's public interface.
"""
import argparse
import datetime
import json
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
    service = from_config(cglib.config(), cglib.load_secrets(), log)
    if service is None:
        print("media is disabled or its configuration/API keys are incomplete")
        return 1
    try:
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
