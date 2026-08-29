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
            self.radarr.post("command", {"name": "MoviesSearch",
                                         "movieIds": [movie_id]})
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
                addOptions={"searchForMovie": True, "addMethod": "manual"},
            )
            movie = self._one(self.radarr.post("movie", payload),
                              "Radarr", "created movie")
            movie_id = int(movie["id"])
            title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
            baseline_file_id = None
        return self._submission("movie", movie_id, title, tmdb_id, preset,
                                profile_name, False,
                                baseline_file_id=baseline_file_id)

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
            self.sonarr.post("command", {"name": "SeriesSearch",
                                         "seriesId": series_id})
            return
        for season in seasons:
            self.sonarr.post("command", {"name": "SeasonSearch",
                                         "seriesId": series_id,
                                         "seasonNumber": season})

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

        if existing is not None:
            series = dict(existing)
            series_id = int(series["id"])
            title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
            series["qualityProfileId"] = profile_id
            series = self._set_series_seasons(series, seasons)
            self.sonarr.put(f"series/{series_id}", series)
            observation = self.observe_series(series_id, seasons)
            if observation["complete"]:
                return self._submission("series", series_id, title, tvdb_id,
                                        preset, profile_name, True, seasons)
            self._search_series(series_id, seasons)
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
                    "searchForMissingEpisodes": seasons is None,
                    "searchForCutoffUnmetEpisodes": False,
                },
            )
            series = self._one(self.sonarr.post("series", payload),
                               "Sonarr", "created series")
            series_id = int(series["id"])
            title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
            if seasons is not None:
                series["qualityProfileId"] = profile_id
                series = self._set_series_seasons(series, seasons)
                self.sonarr.put(f"series/{series_id}", series)
                self._search_series(series_id, seasons)
        return self._submission("series", series_id, title, tvdb_id, preset,
                                profile_name, False, seasons)

    @staticmethod
    def _submission(kind, external_ref, title, catalog_id, preset, profile,
                    already_available, seasons=None, baseline_file_id=None):
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

    def observe_movie(self, movie_id, baseline_file_id=None):
        movie = self._one(self.radarr.get(f"movie/{int(movie_id)}"),
                          "Radarr", "movie")
        if movie.get("hasFile"):
            if baseline_file_id is None:
                return {"complete": True, "progress": {"percent": 100},
                        "detail": "Radarr reports the movie imported"}
            rows = self.radarr.get("moviefile", {"movieId": int(movie_id)})
            if not isinstance(rows, list) or not rows:
                raise MediaError("Radarr reports a movie file but did not return it")
            try:
                current_file_id = int(rows[0]["id"])
            except (KeyError, TypeError, ValueError) as e:
                raise MediaError("Radarr movie file has no id") from e
            if current_file_id != int(baseline_file_id):
                return {"complete": True, "progress": {"percent": 100},
                        "detail": "Radarr imported the requested movie upgrade"}
        percent = self._queue_percent(self.radarr, "movieId", int(movie_id))
        progress = {} if percent is None else {"percent": percent}
        detail = (f"download is {percent}% complete" if percent is not None
                  else "waiting for Radarr to import the requested movie file")
        return {"complete": False, "progress": progress, "detail": detail}

    def observe_series(self, series_id, seasons=None, now=None):
        seasons = self._seasons(seasons) if seasons is not None else None
        rows = self.sonarr.get("episode", {"seriesId": int(series_id)})
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
            if not episode.get("monitored"):
                continue
            aired = self._parse_time(episode.get("airDateUtc"))
            if aired is None or aired > now:
                continue
            targets.append(episode)
        total = len(targets)
        ready = sum(1 for episode in targets if episode.get("hasFile"))
        percent = round(ready * 100 / total) if total else 0
        progress = {"episodes": ready, "total_episodes": total,
                    "percent": percent}
        if not total:
            detail = "no requested monitored episodes have aired yet"
            complete = False
        else:
            detail = f"{ready} of {total} aired episodes are ready"
            complete = ready == total
        return {"complete": complete, "progress": progress, "detail": detail}

    @staticmethod
    def _queue_percent(client, id_key, wanted_id):
        queue = client.get("queue", {"page": 1, "pageSize": 1000})
        if not isinstance(queue, dict):
            return None
        records = [r for r in queue.get("records", [])
                   if isinstance(r, dict)
                   and int(r.get(id_key, 0) or 0) == wanted_id]
        size = sum(float(r.get("size", 0) or 0) for r in records)
        left = sum(float(r.get("sizeleft", 0) or 0) for r in records)
        if size <= 0:
            return None
        return max(0, min(100, round((size - left) * 100 / size)))

    def observe(self, operation, now=None):
        kind = operation.get("kind")
        external_ref = int(operation["external_ref"])
        if kind == "movie_acquisition":
            metadata = operation.get("metadata") or {}
            return self.observe_movie(external_ref,
                                      metadata.get("baseline_file_id"))
        if kind == "series_acquisition":
            metadata = operation.get("metadata") or {}
            return self.observe_series(external_ref, metadata.get("seasons"), now)
        raise MediaError(f"unsupported media operation kind {kind}")

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
                 "baseline_file_id")
                if k in submission}
    try:
        operation = store.track_external(
            submission["kind"], submission["authority"],
            submission["external_ref"], submission["title"],
            detail=f"{submission['authority'].title()} accepted the request",
            metadata=metadata)
        return {**submission, "operation_id": operation["id"]}
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
            print("request not submitted; repeat with --execute")
            return 2
        else:
            import operations
            store = operations.OperationStore(log)
            if args.command == "request-movie":
                result = _track(store, service.request_movie(
                    args.tmdb_id, args.preset))
            else:
                result = _track(store, service.request_series(
                    args.tvdb_id, args.preset, args.seasons))
        print(json.dumps(result, indent=2))
        if args.command == "validate" and not result["ok"]:
            return 1
        return 0
    except MediaError as e:
        print(f"media request failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
