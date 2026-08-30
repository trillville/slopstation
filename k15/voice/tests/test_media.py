"""Blind test: Radarr/Sonarr API boundary, policy, and completion evidence."""
import datetime
import json
import urllib.parse
from pathlib import Path

import _bootstrap  # noqa: F401

import cglib
import media


class FakeArr:
    def __init__(self, name):
        self.name = name
        self.profiles = []
        self.lookup = []
        self.lookup_by_id = None
        self.library = []
        self.episodes = []
        self.movie_files = []
        self.queue = {"records": []}
        self.root_folders = []
        self.status_row = {"version": "1.2.3", "appName": name}
        self.posts = []
        self.puts = []
        self.created = None

    def get(self, endpoint, params=None):
        if endpoint == "qualityprofile":
            return list(self.profiles)
        if endpoint in ("movie/lookup", "series/lookup"):
            if params and str(params.get("term", "")).startswith("tvdb:"):
                return list(self.lookup)
            return list(self.lookup)
        if endpoint == "movie/lookup/tmdb":
            return dict(self.lookup_by_id)
        if endpoint in ("movie", "series"):
            return [dict(row) for row in self.library]
        if endpoint.startswith("movie/"):
            wanted = int(endpoint.split("/")[1])
            return next(dict(row) for row in self.library if row["id"] == wanted)
        if endpoint == "episode":
            return [dict(row) for row in self.episodes]
        if endpoint == "moviefile":
            return [dict(row) for row in self.movie_files]
        if endpoint == "queue":
            return self.queue
        if endpoint == "system/status":
            return dict(self.status_row)
        if endpoint == "rootfolder":
            return [dict(row) for row in self.root_folders]
        raise AssertionError((self.name, "GET", endpoint, params))

    def post(self, endpoint, payload):
        self.posts.append((endpoint, json.loads(json.dumps(payload))))
        if endpoint in ("movie", "series"):
            return dict(self.created)
        if endpoint == "command":
            return {"id": 1}
        raise AssertionError((self.name, "POST", endpoint, payload))

    def put(self, endpoint, payload):
        self.puts.append((endpoint, json.loads(json.dumps(payload))))
        if endpoint == "episode/monitor":
            episode_ids = set(payload["episodeIds"])
            for episode in self.episodes:
                if episode.get("id") in episode_ids:
                    episode["monitored"] = bool(payload["monitored"])
        return dict(payload)


def service():
    cfg = {
        "movieRoot": "/data/Movies",
        "seriesRoot": "/data/TV",
        "moviePresets": {"default": "Movie UHD", "1080p": "Movie HD",
                         "2160p": "Movie UHD"},
        "seriesPresets": {"default": "Series HD", "1080p": "Series HD",
                          "2160p": "Series UHD"},
    }
    radarr, sonarr = FakeArr("Radarr"), FakeArr("Sonarr")
    radarr.profiles = [{"id": 10, "name": "Movie UHD"},
                       {"id": 11, "name": "Movie HD"}]
    sonarr.profiles = [{"id": 20, "name": "Series HD"},
                       {"id": 21, "name": "Series UHD"}]
    radarr.root_folders = [{"id": 1, "path": "/data/Movies/"}]
    sonarr.root_folders = [{"id": 1, "path": "/data/TV"}]
    return media.MediaService(cfg, cglib.CapturingLog("voice"), radarr, sonarr)


def main():
    # --- authenticated HTTP shape -------------------------------------------
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        return {"ok": True}

    client = media.ArrClient("Radarr", "http://127.0.0.1:7878/", "secret-key",
                             transport=transport)
    assert client.get("movie/lookup", {"term": "Dune 2021"}) == {"ok": True}
    client.post("command", {"name": "MoviesSearch", "movieIds": [7]})
    assert calls[0][0] == "GET" and calls[0][2]["X-Api-Key"] == "secret-key"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0][1]).query) == {
        "term": ["Dune 2021"]}
    assert json.loads(calls[1][3]) == {"name": "MoviesSearch", "movieIds": [7]}
    print("  HTTP: v3 paths, header authentication, encoded query, JSON body")

    svc = service()
    svc.radarr.lookup = [
        {"tmdbId": n, "title": f"Movie {n}\n", "year": 2000 + n,
         "status": "released"} for n in range(1, 8)]
    found = svc.find("movie", "Movie")
    assert len(found) == 5 and found[0] == {
        "tmdb_id": 1, "title": "Movie 1", "year": 2001,
        "status": "released"}
    assert "overview" not in found[0]
    print("  lookup: five structured catalog candidates, no release text")

    # --- movies --------------------------------------------------------------
    svc.radarr.lookup_by_id = {"tmdbId": 438631, "title": "Dune", "year": 2021}
    svc.radarr.created = {"id": 31, "tmdbId": 438631, "title": "Dune",
                          "hasFile": False}
    submitted = svc.request_movie(438631)
    endpoint, payload = svc.radarr.posts[0]
    assert endpoint == "movie" and payload["rootFolderPath"] == "/data/Movies"
    assert payload["qualityProfileId"] == 10
    assert payload["addOptions"] == {"searchForMovie": True, "addMethod": "manual"}
    assert submitted["external_ref"] == "31" and not submitted["already_available"]

    svc = service()
    svc.radarr.library = [{"id": 32, "tmdbId": 438631, "title": "Dune",
                           "qualityProfileId": 10, "hasFile": False}]
    changed = svc.request_movie(438631, "1080p")
    assert changed["profile"] == "Movie HD"
    assert svc.radarr.puts[0][1]["qualityProfileId"] == 11
    assert svc.radarr.posts[-1] == (
        "command", {"name": "MoviesSearch", "movieIds": [32]})
    svc.radarr.library[0]["hasFile"] = True
    before = len(svc.radarr.posts)
    ready = svc.request_movie(438631)
    assert ready["already_available"] and len(svc.radarr.posts) == before

    svc = service()
    svc.radarr.library = [{"id": 33, "tmdbId": 438631, "title": "Dune",
                           "qualityProfileId": 11, "hasFile": True}]
    svc.radarr.movie_files = [{"id": 71, "movieId": 33}]
    upgrade = svc.request_movie(438631, "2160p")
    assert not upgrade["already_available"]
    assert upgrade["baseline_file_id"] == 71
    assert svc.radarr.puts[0][1]["qualityProfileId"] == 10
    operation = {"kind": "movie_acquisition", "external_ref": "33",
                 "metadata": {"baseline_file_id": 71}}
    assert not svc.observe(operation)["complete"]
    svc.radarr.movie_files[0]["id"] = 72
    assert svc.observe(operation)["complete"]
    print("  movie: default add, steering, same-profile no-op, tracked 4K upgrade")

    # --- selected series seasons -------------------------------------------
    svc = service()
    svc.sonarr.lookup = [{
        "tvdbId": 81189, "title": "Breaking Bad", "year": 2008,
        "seasons": [
            {"seasonNumber": 0, "monitored": False},
            {"seasonNumber": 1, "monitored": False},
            {"seasonNumber": 2, "monitored": False},
        ],
    }]
    svc.sonarr.created = {
        "id": 41, "tvdbId": 81189, "title": "Breaking Bad",
        "qualityProfileId": 20,
        "seasons": svc.sonarr.lookup[0]["seasons"],
    }
    series = svc.request_series(81189, "default", [2])
    added = svc.sonarr.posts[0][1]
    assert added["rootFolderPath"] == "/data/TV"
    assert added["addOptions"]["monitor"] == "none"
    monitored = {r["seasonNumber"]: r["monitored"]
                 for r in svc.sonarr.puts[0][1]["seasons"]}
    assert monitored == {0: False, 1: False, 2: True}
    assert not [post for post in svc.sonarr.posts if post[0] == "command"]
    assert series["seasons"] == [2] and series["search_pending"]
    pending = {
        "kind": "series_acquisition", "external_ref": "41",
        "metadata": {"seasons": [2], "search_pending": True},
    }
    assert not svc.dispatch_pending_series_search(pending)
    svc.sonarr.episodes = [
        {"id": 101, "seasonNumber": 0, "monitored": False,
         "hasFile": False, "airDateUtc": "2019-01-01T00:00:00Z"},
        {"id": 102, "seasonNumber": 2, "monitored": False,
         "hasFile": False, "airDateUtc": "2020-01-01T00:00:00Z"},
        {"id": 103, "seasonNumber": 2, "monitored": True,
         "hasFile": False, "airDateUtc": "2020-01-08T00:00:00Z"},
    ]
    assert svc.dispatch_pending_series_search(pending)
    assert svc.sonarr.puts[-1] == (
        "episode/monitor", {"episodeIds": [102], "monitored": True})
    assert svc.sonarr.posts[-1] == (
        "command", {"name": "SeasonSearch", "seriesId": 41,
                    "seasonNumber": 2})
    observation = svc.observe_series(41, [2])
    assert observation["progress"] == {
        "episodes": 0, "total_episodes": 2, "percent": 0}
    try:
        svc.request_series(81189, seasons=[0])
        raise AssertionError("specials accepted as a normal season")
    except media.MediaError:
        pass

    svc = service()
    svc.sonarr.library = [{
        "id": 42, "tvdbId": 81189, "title": "Breaking Bad",
        "qualityProfileId": 20,
        "seasons": [{"seasonNumber": 1, "monitored": True}],
    }]
    svc.sonarr.episodes = [
        {"id": 101, "episodeFileId": 201, "seasonNumber": 1,
         "monitored": True, "hasFile": True,
         "airDateUtc": "2020-01-01T00:00:00Z"},
        {"id": 102, "episodeFileId": 0, "seasonNumber": 1,
         "monitored": True, "hasFile": False,
         "airDateUtc": "2020-01-08T00:00:00Z"},
    ]
    series_upgrade = svc.request_series(81189, "2160p", [1])
    assert series_upgrade["baseline_episode_files"] == {"101": 201}
    upgrade_operation = {
        "kind": "series_acquisition", "external_ref": "42",
        "metadata": {"seasons": [1],
                     "baseline_episode_files": {"101": 201}},
    }
    assert not svc.observe(upgrade_operation)["complete"]
    svc.sonarr.episodes[0]["episodeFileId"] = 301
    svc.sonarr.episodes[1].update(hasFile=True, episodeFileId=302)
    assert svc.observe(upgrade_operation)["complete"]
    print("  series: selected seasons, implicit specials excluded, upgrades tracked")

    # --- positive completion evidence --------------------------------------
    svc = service()
    svc.radarr.library = [{"id": 50, "tmdbId": 1, "title": "Arrival",
                           "hasFile": False}]
    svc.radarr.queue = {"records": [
        {"movieId": 50, "size": 1000, "sizeleft": 250,
         "title": "UNTRUSTED RELEASE TEXT"}]}
    movie_progress = svc.observe_movie(50)
    assert not movie_progress["complete"]
    assert movie_progress["progress"] == {"percent": 75}
    assert "UNTRUSTED" not in movie_progress["detail"]
    svc.radarr.library[0]["hasFile"] = True
    assert svc.observe_movie(50)["complete"]

    now = datetime.datetime(2026, 8, 29, tzinfo=datetime.timezone.utc)
    empty = svc.observe_series(60, [1], now)
    assert not empty["metadata_ready"]
    assert empty["detail"] == "Sonarr is still populating episode metadata"
    svc.sonarr.episodes = [
        {"seasonNumber": 0, "monitored": True, "hasFile": False,
         "airDateUtc": "2020-01-01T00:00:00Z"},
        {"seasonNumber": 1, "monitored": True, "hasFile": True,
         "airDateUtc": "2020-01-01T00:00:00Z"},
        {"seasonNumber": 1, "monitored": True, "hasFile": False,
         "airDateUtc": "2020-01-08T00:00:00Z"},
        {"seasonNumber": 1, "monitored": True, "hasFile": False,
         "airDateUtc": "2027-01-01T00:00:00Z"},
        {"seasonNumber": 2, "monitored": False, "hasFile": False,
         "airDateUtc": "2020-01-01T00:00:00Z"},
    ]
    progress = svc.observe_series(60, None, now)
    assert progress["metadata_ready"]
    assert not progress["complete"]
    assert progress["progress"] == {"episodes": 1, "total_episodes": 2,
                                    "percent": 50}
    svc.sonarr.episodes[2]["hasFile"] = True
    assert svc.observe_series(60, None, now)["complete"]
    print("  observe: queue bytes ignored except percent; only aired monitored files complete")

    # --- factory gating and status ------------------------------------------
    cfg = json.loads(json.dumps(_bootstrap.CONFIG))
    log = cglib.CapturingLog("voice")
    assert media.from_config(cfg, {}, log) is None
    cfg["media"]["enabled"] = True
    assert media.from_config(cfg, {}, log) is None
    assert log.find("lane_disabled")[-1]["what"] == "media"
    status = service().status()
    assert status["radarr"]["version"] == "1.2.3"
    validation_service = service()
    validation = validation_service.validate()
    assert validation["ok"] and validation["checks"]["movie"]["root_exists"]
    validation_service.radarr.profiles.pop()
    invalid = validation_service.validate()
    assert not invalid["ok"]
    assert invalid["checks"]["movie"]["missing_profiles"] == ["Movie HD"]
    failed_submission = {
        "ok": True, "kind": "movie_acquisition", "authority": "radarr",
        "external_ref": "31", "title": "Dune", "catalog_id": 438631,
        "preset": "default", "profile": "Movie UHD",
        "already_available": False,
    }

    class FailingStore:
        log = cglib.CapturingLog("voice")

        def track_external(self, *args, **kwargs):
            raise OSError("disk unavailable")

    assert media._track(FailingStore(), failed_submission)["tracking"] == "failed"
    print("  factory: disabled is inert; preflight checks roots and named profiles")

    root = Path(__file__).resolve().parents[3]
    compose = (root / "k15" / "media" / "compose.yaml").read_text(encoding="utf-8")
    start_media = (root / "k15" / "media" / "Start-Media.ps1").read_text(
        encoding="utf-8")
    for sidecar in ("flaresolverr:", "prowlarr:", "radarr:", "sonarr:"):
        assert sidecar in compose
    assert "qbittorrent:" not in compose
    assert "ghcr.io/flaresolverr/flaresolverr:latest" in compose
    assert "8191:8191" not in compose
    assert compose.count("source: ${MEDIA_ROOT}") == 2
    assert "127.0.0.1:7878:7878" in compose
    assert "127.0.0.1:8989:8989" in compose
    assert "--remove-orphans" in start_media
    assert "logs qbittorrent" not in start_media
    assert _bootstrap.CONFIG["media"]["movieRoot"] == "/data/Movies"
    assert _bootstrap.CONFIG["media"]["seriesRoot"] == "/data/TV"
    print("  deployment: Arr sidecars share /data; native qBittorrent stays VPN-bound")

    print("OK - media: authenticated APIs, lookup/request policy, seasons, and completion")


if __name__ == "__main__":
    main()
