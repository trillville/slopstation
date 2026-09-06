"""Test the movie and TV management tools over fake Radarr, Sonarr, Prowlarr."""

import datetime
import types

import pytest

from helpers import CapturingLog
from slopstation.agent.llm import assistant
from slopstation.agent.tools import media, operations
from slopstation.agent.tools.media_clients import MediaError

NOW = datetime.datetime.now(datetime.UTC)


def iso(days):
    return (NOW + datetime.timedelta(days=days)).isoformat()


class Arr:
    """Answers GETs from a dict of endpoint -> value (callables see params)
    and records every write."""

    def __init__(self, name, **answers):
        self.name = name
        self.answers = answers
        self.gets, self.posts, self.puts, self.deletes = [], [], [], []

    def get(self, endpoint, params=None):
        self.gets.append((endpoint, params))
        value = self.answers.get(endpoint)
        if value is None:
            raise MediaError(f"{self.name}: unexpected GET {endpoint}")
        return value(params or {}) if callable(value) else value

    def post(self, endpoint, payload):
        self.posts.append((endpoint, payload))
        return {"id": 99}

    def put(self, endpoint, payload):
        self.puts.append((endpoint, payload))
        return payload

    def delete(self, endpoint, params=None):
        self.deletes.append((endpoint, params))


@pytest.fixture
def stack():
    radarr = Arr(
        "Radarr",
        movie=[
            {
                "id": 1,
                "tmdbId": 438631,
                "title": "Dune",
                "year": 2021,
                "hasFile": True,
                "sizeOnDisk": 60 * 1024**3,
                "monitored": True,
                "added": "2026-08-01T00:00:00Z",
                "genres": ["Sci-Fi"],
                "qualityProfileId": 10,
                "path": "/data/Movies/Dune (2021)",
            },
            {
                "id": 2,
                "tmdbId": 348,
                "title": "Alien",
                "year": 1979,
                "hasFile": False,
                "sizeOnDisk": 0,
                "monitored": False,
                "added": "2026-09-01T00:00:00Z",
                "genres": ["Horror"],
                "qualityProfileId": 11,
            },
        ],
        qualityprofile=[
            {"id": 10, "name": "Movie UHD"},
            {"id": 11, "name": "Movie HD"},
        ],
        **{
            "movie/1": {"id": 1, "tmdbId": 438631, "title": "Dune", "hasFile": True},
            "movie/2": {"id": 2, "tmdbId": 348, "title": "Alien", "hasFile": False},
        },
        moviefile=lambda p: (
            [
                {
                    "id": 501,
                    "relativePath": "Dune.mkv",
                    "size": 60 * 1024**3,
                    "quality": {"quality": {"name": "Remux-2160p"}},
                }
            ]
            if p.get("movieId") == 1
            else []
        ),
        **{
            "wanted/missing": {
                "totalRecords": 1,
                "records": [{"title": "Alien", "year": 1979, "tmdbId": 348}],
            }
        },
        **{"wanted/cutoff": {"totalRecords": 0, "records": []}},
        calendar=[
            {"title": "Alien", "year": 1979, "digitalRelease": iso(2), "hasFile": False}
        ],
        release=lambda p: [
            {
                "guid": "g1",
                "indexerId": 3,
                "indexer": "IX",
                "title": "Dune.2021.2160p.REMUX-GRP",
                "size": 60 * 1024**3,
                "seeders": 40,
                "quality": {"quality": {"name": "Remux-2160p"}},
                "ageHours": 5,
                "approved": True,
                "rejections": [],
            },
            {
                "guid": "g2",
                "indexerId": 3,
                "indexer": "IX",
                "title": "Dune.2021.CAM",
                "size": 1024**3,
                "seeders": 900,
                "quality": {"quality": {"name": "CAM"}},
                "ageHours": 1,
                "approved": False,
                "rejections": ["quality not wanted"],
            },
        ],
        queue=lambda p: {
            "records": [
                {
                    "id": 7,
                    "movie": {"title": "Alien"},
                    "status": "warning",
                    "trackedDownloadState": "importPending",
                    "trackedDownloadStatus": "warning",
                    "size": 100.0,
                    "sizeleft": 0.0,
                    "statusMessages": [
                        {
                            "title": "Alien.1979.mkv",
                            "messages": ["No files found are eligible for import"],
                        }
                    ],
                    "downloadId": "ABC123",
                }
            ]
        },
        # Honours the server-side event filter: a grab is event 1.
        history=lambda p: {
            "totalRecords": 1 if 1 in (p.get("eventType") or [1]) else 0,
            "records": [
                {
                    "date": "2026-09-04T10:00:00Z",
                    "eventType": "grabbed",
                    "movie": {"title": "Alien"},
                    "sourceTitle": "Alien.1979.REL",
                    "quality": {"quality": {"name": "Bluray-1080p"}},
                }
            ]
            if 1 in (p.get("eventType") or [1])
            else [],
        },
        health=[
            {
                "type": "warning",
                "source": "IndexerStatusCheck",
                "message": "Indexers unavailable: IX",
            }
        ],
        collection=[
            {
                "title": "Alien Collection",
                "monitored": True,
                "movies": [
                    {"tmdbId": 348, "title": "Alien"},
                    {"tmdbId": 679, "title": "Aliens"},
                ],
            }
        ],
        manualimport=lambda p: (
            [
                {
                    "path": "/data/torrents/Alien/Alien.1979.mkv",
                    "relativePath": "Alien.1979.mkv",
                    "quality": {"quality": {"name": "Bluray-1080p"}},
                    "languages": [],
                    "movie": {"id": 2},
                }
            ]
            if p.get("downloadId") == "ABC123"
            else [{"path": "/x", "relativePath": "x.mkv", "movie": {}}]
        ),
    )
    sonarr = Arr(
        "Sonarr",
        series=[
            {
                "id": 5,
                "tvdbId": 81189,
                "title": "Breaking Bad",
                "year": 2008,
                "monitored": True,
                "status": "ended",
                "added": "2026-07-01T00:00:00Z",
                "genres": ["Drama"],
                "qualityProfileId": 20,
                "path": "/data/TV/Breaking Bad",
                "statistics": {
                    "episodeFileCount": 3,
                    "totalEpisodeCount": 5,
                    "sizeOnDisk": 9 * 1024**3,
                },
                "seasons": [
                    {"seasonNumber": 0, "monitored": False},
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": True},
                ],
            },
        ],
        qualityprofile=[
            {"id": 20, "name": "Series HD"},
            {"id": 21, "name": "Series UHD"},
        ],
        episode=lambda p: [
            {
                "id": 101,
                "seasonNumber": 1,
                "episodeNumber": 1,
                "hasFile": True,
                "episodeFileId": 1001,
                "monitored": True,
                "airDateUtc": iso(-400),
            },
            {
                "id": 102,
                "seasonNumber": 1,
                "episodeNumber": 2,
                "hasFile": False,
                "monitored": True,
                "airDateUtc": iso(-390),
            },
            {
                "id": 201,
                "seasonNumber": 2,
                "episodeNumber": 1,
                "hasFile": False,
                "monitored": True,
                "airDateUtc": iso(3),
            },
        ],
        **{
            "wanted/missing": {
                "totalRecords": 1,
                "records": [
                    {
                        "series": {"title": "Breaking Bad"},
                        "seasonNumber": 1,
                        "episodeNumber": 2,
                        "title": "Cat's in the Bag",
                        "airDateUtc": iso(-390),
                    }
                ],
            }
        },
        **{"wanted/cutoff": {"totalRecords": 0, "records": []}},
        calendar=[
            {
                "series": {"title": "Breaking Bad"},
                "seasonNumber": 2,
                "episodeNumber": 1,
                "title": "Seven Thirty-Seven",
                "airDateUtc": iso(3),
                "hasFile": False,
            }
        ],
        release=lambda p: [
            {
                "guid": "s1",
                "indexerId": 4,
                "indexer": "IX",
                "title": "Breaking.Bad.S01.1080p",
                "size": 9 * 1024**3,
                "seeders": 12,
                "quality": {"quality": {"name": "Bluray-1080p"}},
                "ageHours": 800,
                "approved": True,
                "rejections": [],
                "params": dict(p),
            }
        ],
        queue=lambda p: {"records": []},
        history=lambda p: {"records": []},
        health=[],
    )
    prowlarr = Arr(
        "Prowlarr",
        health=[],
        indexer=[{"id": 3, "name": "IX"}],
        indexerstatus=[
            {
                "indexerId": 3,
                "disabledTill": "2026-09-06T00:00:00Z",
                "mostRecentFailure": "2026-09-05T00:00:00Z",
            }
        ],
        search=lambda p: [
            {
                "title": "Some.Documentary.2025.1080p",
                "indexer": "IX",
                "size": 4 * 1024**3,
                "seeders": 30,
                "age": 12,
                "guid": "d1",
                "indexerId": 3,
                "params": dict(p),
            }
        ],
    )
    cfg = {
        "movieRoot": "/data/Movies",
        "seriesRoot": "/data/TV",
        "moviePresets": {
            "default": "Movie UHD",
            "1080p": "Movie HD",
            "2160p": "Movie UHD",
        },
        "seriesPresets": {
            "default": "Series HD",
            "1080p": "Series HD",
            "2160p": "Series UHD",
        },
    }
    svc = media.MediaService(
        cfg, CapturingLog("voice"), radarr, sonarr, prowlarr=prowlarr, qbit=None
    )
    return svc, radarr, sonarr, prowlarr


@pytest.fixture
def rig(stack):
    svc, *_ = stack
    log = CapturingLog("voice")
    dispatch = types.SimpleNamespace(
        dry_run=False, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    tk = assistant.Toolkit(dispatch, log, media=svc)
    tk.load([s.name for s in assistant.REGISTRY if s.area == "media"])
    return tk, dispatch, log


def test_browse_details_missing_and_calendar(rig):
    tk, _, _ = rig
    recent = tk.call("browse_media", {"kind": "movie"})
    assert recent["ok"] and [i["title"] for i in recent["items"]] == ["Alien", "Dune"]
    assert (
        tk.call("browse_media", {"kind": "movie", "sort": "largest"})["items"][0][
            "title"
        ]
        == "Dune"
    )
    assert [
        i["title"]
        for i in tk.call("browse_media", {"kind": "movie", "genre": "horror"})["items"]
    ] == ["Alien"]
    assert [
        i["title"]
        for i in tk.call("browse_media", {"kind": "movie", "unmonitored_only": True})[
            "items"
        ]
    ] == ["Alien"]
    shows = tk.call("browse_media", {"kind": "series", "sort": "title"})
    assert (
        shows["items"][0]["episodes_held"] == 3 and shows["items"][0]["size_gb"] == 9.0
    )
    assert not tk.call("browse_media", {"kind": "movie", "sort": "colour"})["ok"]
    dune = tk.call("media_details", {"kind": "movie", "catalog_id": 438631})
    assert (
        dune["ok"]
        and dune["quality_profile"] == "Movie UHD"
        and dune["files"][0]["quality"] == "Remux-2160p"
    )
    bb = tk.call("media_details", {"kind": "series", "catalog_id": 81189})
    assert bb["seasons"] == [
        {"season": 1, "held": 1, "missing": 1, "upcoming": 0, "monitored": True},
        {"season": 2, "held": 0, "missing": 0, "upcoming": 1, "monitored": True},
    ]
    assert not tk.call("media_details", {"kind": "movie", "catalog_id": 1})["ok"]
    missing = tk.call("missing_media", {"kind": "series"})
    assert missing["missing_count"] == 1 and missing["missing"][0]["episode"] == 2
    cal = tk.call("calendar", {"kind": "both", "days": 7})
    assert cal["count"] == 2 and [r["kind"] for r in cal["items"]] == [
        "movie",
        "episode",
    ]
    assert cal["items"][1]["series"] == "Breaking Bad"


def test_search_releases_and_grab(rig, stack):
    tk, _, _ = rig
    _, radarr, sonarr, _ = stack
    out = tk.call("search_releases", {"kind": "movie", "catalog_id": 438631})
    assert out["ok"] and out["title"] == "Dune" and out["count"] == 2
    # Approved first, even with fewer seeders; the rejection travels.
    assert [r["guid"] for r in out["releases"]] == ["g1", "g2"]
    assert out["releases"][1]["rejections"] == ["quality not wanted"]
    assert not tk.call("search_releases", {"kind": "series", "catalog_id": 81189})[
        "ok"
    ], "a season is needed"
    season = tk.call(
        "search_releases", {"kind": "series", "catalog_id": 81189, "season": 1}
    )
    assert season["releases"][0]["name"].startswith("Breaking.Bad")
    # What the tool actually asked Sonarr for.
    assert sonarr.gets[-1] == ("release", {"seriesId": 5, "seasonNumber": 1})
    ep = tk.call(
        "search_releases",
        {"kind": "series", "catalog_id": 81189, "season": 1, "episode": 2},
    )
    assert ep["ok"]
    # A special (season 0) is found too.
    sonarr.answers["episode"] = lambda p: [
        {
            "id": 900,
            "seasonNumber": 0,
            "episodeNumber": 1,
            "hasFile": False,
            "monitored": True,
        }
    ]
    special = tk.call(
        "search_releases",
        {"kind": "series", "catalog_id": 81189, "season": 0, "episode": 1},
    )
    assert special["ok"] and sonarr.gets[-1] == ("release", {"episodeId": 900})
    assert not tk.call(
        "search_releases",
        {"kind": "series", "catalog_id": 81189, "season": 1, "episode": 9},
    )["ok"]
    grab = {"kind": "movie", "catalog_id": 438631, "guid": "g1", "indexer_id": 3}
    grabbed = tk.call("grab_release", grab)
    assert grabbed["ok"] and radarr.posts[-1] == (
        "release",
        {"guid": "g1", "indexerId": 3},
    )
    assert grabbed["phase"] == "grabbed" and grabbed["title"] == "Dune"
    assert not tk.call("grab_release", {**grab, "guid": ""})["ok"]
    assert not tk.call("grab_release", {**grab, "indexer_id": "x"})["ok"]
    assert (
        "not in the library"
        in tk.call("grab_release", {**grab, "catalog_id": 1})["error"]
    )
    # The app forgets its search results after half an hour.
    radarr.post = lambda endpoint, payload: (_ for _ in ()).throw(
        MediaError("Radarr: HTTP 404 for release")
    )
    assert "search_releases again" in tk.call("grab_release", grab)["error"]


def test_retry_monitor_and_profile_changes(rig, stack):
    tk, _, _ = rig
    _, radarr, sonarr, _ = stack
    out = tk.call("retry_search", {"kind": "movie", "catalog_id": 348})
    assert out["ok"] and out["phase"] == "searching" and out["command_ids"] == [99]
    assert radarr.posts[-1] == ("command", {"name": "MoviesSearch", "movieIds": [2]})
    out = tk.call("retry_search", {"kind": "series", "catalog_id": 81189, "season": 2})
    # A season search is the app's own season search, watched for exactly
    # that season's episodes; one episode is an episode search.
    assert out["ok"] and out["episode_ids"] == [201] and "seasons" not in out
    assert sonarr.posts[-1][1] == {
        "name": "SeasonSearch",
        "seriesId": 5,
        "seasonNumber": 2,
    }
    one = tk.call(
        "retry_search",
        {"kind": "series", "catalog_id": 81189, "season": 1, "episode": 2},
    )
    assert one["episode_ids"] == [102] and one["promise"] == "search"
    assert sonarr.posts[-1][1] == {"name": "EpisodeSearch", "episodeIds": [102]}
    assert (
        "no season 4"
        in tk.call(
            "retry_search", {"kind": "series", "catalog_id": 81189, "season": 4}
        )["error"]
    )
    out = tk.call("retry_search", {"kind": "series", "catalog_id": 81189})
    assert out["ok"] and sonarr.posts[-1][1]["name"] == "SeriesSearch"
    assert not tk.call("retry_search", {"kind": "series", "catalog_id": "x"})["ok"]
    # Unmonitor one season, leaving the rest.
    out = tk.call(
        "set_monitored",
        {"kind": "series", "catalog_id": 81189, "monitored": False, "seasons": [2]},
    )
    assert out["ok"] and out["scope"] == "seasons [2]"
    put = sonarr.puts[-1][1]
    assert [s["monitored"] for s in put["seasons"]] == [False, True, False] and put[
        "monitored"
    ] is True
    # The app's own row is not mutated underneath.
    assert [s["monitored"] for s in sonarr.answers["series"][0]["seasons"]] == [
        False,
        True,
        True,
    ]
    # Season zero (the specials) is a season: unmonitoring it must not be a
    # silent no-op.
    out = tk.call(
        "set_monitored",
        {"kind": "series", "catalog_id": 81189, "monitored": False, "seasons": [0]},
    )
    assert out["ok"]
    put = sonarr.puts[-1][1]
    assert [s["monitored"] for s in put["seasons"]] == [False, True, True]
    sonarr.answers["series"][0]["seasons"][0]["monitored"] = True
    out = tk.call(
        "set_monitored",
        {"kind": "series", "catalog_id": 81189, "monitored": False, "seasons": [0]},
    )
    assert [s["monitored"] for s in sonarr.puts[-1][1]["seasons"]][0] is False
    # The whole series: only its own flag moves, as the app's UI does; the
    # seasons (and so every episode) are left as the user set them.
    whole = tk.call(
        "set_monitored", {"kind": "series", "catalog_id": 81189, "monitored": False}
    )
    assert whole["ok"] and whole["scope"] == "everything"
    put = sonarr.puts[-1][1]
    assert put["monitored"] is False
    # Season 0 was set monitored above; the whole-series call left it alone.
    assert [s["monitored"] for s in put["seasons"]] == [True, True, True]
    assert tk.call(
        "set_monitored", {"kind": "movie", "catalog_id": 348, "monitored": True}
    )["ok"]
    assert radarr.puts[-1] == (
        "movie/2",
        dict(radarr.answers["movie"][1], monitored=True),
    )
    assert not tk.call(
        "set_monitored", {"kind": "movie", "catalog_id": 348, "monitored": "yes"}
    )["ok"]
    assert not tk.call(
        "set_monitored",
        {"kind": "movie", "catalog_id": 348, "monitored": True, "seasons": [1]},
    )["ok"]
    prof = tk.call(
        "set_quality_profile", {"kind": "movie", "catalog_id": 348, "preset": "2160p"}
    )
    assert (
        prof["ok"]
        and prof["profile"] == "Movie UHD"
        and prof["changed"]
        and radarr.puts[-1][1]["qualityProfileId"] == 10
    )
    same = tk.call(
        "set_quality_profile",
        {"kind": "movie", "catalog_id": 438631, "preset": "default"},
    )
    assert same["ok"] and same["changed"] is False
    assert not tk.call(
        "set_quality_profile", {"kind": "movie", "catalog_id": 348, "preset": "8k"}
    )["ok"]


def test_queue_resolution_is_gated_and_manual_import_matches(rig, stack):
    tk, dispatch, log = rig
    _, radarr, _, _ = stack
    q = tk.call("import_queue", {})
    assert (
        q["count"] == 1
        and q["items"][0]["title"] == "Alien"
        and q["items"][0]["problem"] == "warning"
    )
    assert q["items"][0]["warnings"] == [
        "Alien.1979.mkv: No files found are eligible for import"
    ]
    assert q["items"][0]["queue_id"] == 7 and q["items"][0]["download_id"] == "ABC123"
    asked = tk.call(
        "resolve_queue_item", {"kind": "movie", "queue_id": 7, "blocklist": True}
    )
    assert (
        not asked["ok"]
        and "Alien" in asked["acknowledgment"]
        and "never takes that release again" in asked["acknowledgment"]
    )
    assert radarr.deletes == []
    dispatch.utterance = types.SimpleNamespace(turn="aa0002", asked="yes")
    done = tk.call(
        "resolve_queue_item", {"kind": "movie", "queue_id": 7, "blocklist": True}
    )
    assert done["ok"] and radarr.deletes[-1] == (
        "queue/7",
        {"removeFromClient": "true", "blocklist": "true"},
    )
    # Forgetting the queue item without touching the torrent needs no confirmation.
    kept = tk.call(
        "resolve_queue_item",
        {"kind": "movie", "queue_id": 7, "remove_from_client": False},
    )
    assert kept["ok"] and radarr.deletes[-1][1]["removeFromClient"] == "false"
    assert not tk.call("resolve_queue_item", {"kind": "movie", "queue_id": 8})["ok"]
    imported = tk.call("manual_import", {"kind": "movie", "download_id": "ABC123"})
    assert imported["ok"] and imported["files"] == 1
    assert imported["phase"] == "importing" and imported["title"] == "Alien"
    cmd = radarr.posts[-1][1]
    assert (
        cmd["name"] == "ManualImport"
        and cmd["files"][0]["movieId"] == 2
        and cmd["importMode"] == "auto"
    )
    unmatched = tk.call("manual_import", {"kind": "movie", "download_id": "ZZZ"})
    assert not unmatched["ok"] and unmatched["unmatched"] == ["x.mkv"]
    # A file the app itself rejects (a sample) blocks the import: ManualImport
    # would take it anyway, and the later file would win.
    radarr.answers["manualimport"] = lambda p: [
        {
            "path": "/data/torrents/Alien/Alien.1979.mkv",
            "relativePath": "Alien.1979.mkv",
            "quality": {"quality": {"name": "Bluray-1080p"}},
            "languages": [],
            "movie": {"id": 2},
        },
        {
            "path": "/data/torrents/Alien/sample.mkv",
            "relativePath": "sample.mkv",
            "quality": {"quality": {"name": "Bluray-1080p"}},
            "languages": [],
            "movie": {"id": 2},
            "rejections": [{"reason": "Sample", "type": "permanent"}],
        },
    ]
    n = len(radarr.posts)
    blocked = tk.call("manual_import", {"kind": "movie", "download_id": "ABC123"})
    assert not blocked["ok"] and blocked["rejected"] == [
        {"file": "sample.mkv", "why": ["Sample"]}
    ]
    assert blocked["matched"] == 1 and len(radarr.posts) == n
    # Blocklisting without removal still erases (the app removes failed
    # downloads itself), so it is gated too, with its own wording.
    dispatch.utterance = types.SimpleNamespace(turn="aa0003", asked="")
    asked = tk.call(
        "resolve_queue_item",
        {
            "kind": "movie",
            "queue_id": 7,
            "remove_from_client": False,
            "blocklist": True,
        },
    )
    assert (
        not asked["ok"] and "never takes that release again" in asked["acknowledgment"]
    )
    # Forgetting alone says where the torrent ends up.
    assert "delete_torrent" in kept["detail"]


def test_history_health_collections_and_indexer_search(rig, stack):
    tk, _, _ = rig
    _, radarr, _, prowlarr = stack
    hist = tk.call("media_history", {"event": "grabbed"})
    assert hist["count"] == 1 and hist["items"][0]["release"] == "Alien.1979.REL"
    # The filter is the app's, by its event ids, so the count is a total.
    assert radarr.gets[-1][1]["eventType"] == [1]
    assert tk.call("media_history", {"event": "deleted"})["count"] == 0
    assert radarr.gets[-1][1]["eventType"] == [6]
    assert not tk.call("media_history", {"event": "exploded"})["ok"]
    health = tk.call("media_health", {})
    assert health["ok"] and not health["healthy"]
    assert (
        health["health"][0]["app"] == "Radarr"
        and "IX" in health["health"][0]["message"]
    )
    assert (
        health["indexers"][0]["indexer"] == "IX"
        and health["indexers"][0]["disabled_till"]
    )
    coll = tk.call("movie_collections", {"name": "alien"})
    assert (
        coll["count"] == 1
        and coll["collections"][0]["held"] == 0
        and coll["collections"][0]["missing"] == ["Alien", "Aliens"]
    )
    found = tk.call(
        "search_indexers", {"query": "some documentary", "category": "movies"}
    )
    assert found["ok"] and found["releases"][0]["indexer"] == "IX"
    assert prowlarr.gets[-1][1]["categories"] == [2000]
    assert not tk.call("search_indexers", {"query": ""})["ok"]


def test_dry_run_and_failures_come_back_as_errors(stack):
    svc, radarr, sonarr, _ = stack
    log = CapturingLog("voice")
    dispatch = types.SimpleNamespace(
        dry_run=True, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    tk = assistant.Toolkit(dispatch, log, media=svc)
    tk.load(["grab_release", "resolve_queue_item", "browse_media", "set_monitored"])
    assert (
        tk.call(
            "grab_release",
            {"kind": "movie", "catalog_id": 438631, "guid": "g1", "indexer_id": 3},
        )["dry_run"]
        and not radarr.posts
    )
    assert (
        tk.call("resolve_queue_item", {"kind": "movie", "queue_id": 7})["dry_run"]
        and not radarr.deletes
    )
    assert (
        tk.call(
            "set_monitored", {"kind": "movie", "catalog_id": 348, "monitored": False}
        )["dry_run"]
        and not radarr.puts
    )
    # A service error is an error dict, never a raised exception into the turn.
    radarr.answers.pop("movie")
    out = tk.call("browse_media", {"kind": "movie"})
    assert not out["ok"] and "unexpected GET movie" in out["error"]
    assert log.find("tool_error")[-1]["tool"] == "browse_media"


def test_work_on_a_held_title_is_tracked_for_exactly_its_scope(stack):
    svc, radarr, sonarr, _ = stack
    log = CapturingLog("voice")
    store = operations.OperationStore(log)
    dispatch = types.SimpleNamespace(
        dry_run=False, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    tk = assistant.Toolkit(dispatch, log, media=svc, operations=store)
    tk.load(["grab_release", "retry_search", "manual_import"])
    radarr.answers["command/99"] = {"id": 99, "status": "started"}
    sonarr.answers["command/1"] = {"id": 1, "status": "started"}
    sonarr.answers["command/99"] = {"id": 99, "status": "completed"}
    # A grab on a title nobody requested is its own operation, with the file
    # it already held as the baseline, so the upgrade is not called done
    # because the old file is still there.
    grabbed = tk.call(
        "grab_release",
        {"kind": "movie", "catalog_id": 438631, "guid": "g1", "indexer_id": 3},
    )
    assert grabbed["ok"] and "joined" not in grabbed
    op = store.get(grabbed["operation_id"])
    assert (op["kind"], op["external_ref"], op["title"]) == (
        "movie_acquisition",
        "1",
        "Dune",
    )
    assert op["progress"] == {"phase": "grabbed"}
    assert op["metadata"]["baseline_file_id"] == 501 and op["turn"] == "aa0001"
    assert "promise" not in op["metadata"], "a grab promises the file"
    # The monitor keeps that phase until the client shows the download.
    seen = svc.observe(op)
    assert not seen["complete"] and seen["progress"]["phase"] == "grabbed"
    # A fresh search for the same title joins the operation rather than
    # opening a second row: the baseline stays, the command is watched, and
    # the stronger promise (the file, not just a search) survives.
    searched = tk.call("retry_search", {"kind": "movie", "catalog_id": 438631})
    assert searched["ok"] and searched["joined"]
    assert searched["operation_id"] == op["id"] and len(store.active()) == 1
    op = store.get(op["id"])
    assert op["progress"]["phase"] == "searching"
    assert op["metadata"]["command_ids"] == [99]
    assert op["metadata"]["baseline_file_id"] == 501
    assert op["metadata"]["promise"] == "acquire"
    # A season-1 request, then a season-2 search: the operation now covers
    # both, and the season-1 search still running stays watched beside the
    # new one. Nothing the user asked for is dropped to keep one row.
    request = store.track_external(
        "series_acquisition",
        "sonarr",
        "5",
        "Breaking Bad",
        metadata={"catalog_id": 81189, "seasons": [1], "command_ids": [1]},
    )
    joined = tk.call(
        "retry_search", {"kind": "series", "catalog_id": 81189, "season": 2}
    )
    assert joined["joined"] and joined["operation_id"] == request["id"]
    metadata = store.get(request["id"])["metadata"]
    assert metadata["seasons"] == [1] and metadata["episode_ids"] == [201]
    assert metadata["command_ids"] == [1, 99] and metadata["promise"] == "acquire"
    # An episode grab on the same series widens the episode scope; the
    # finished season-2 search is dropped, the live season-1 one kept.
    tk.call(
        "grab_release",
        {
            "kind": "series",
            "catalog_id": 81189,
            "guid": "g1",
            "indexer_id": 3,
            "season": 1,
            "episode": 2,
        },
    )
    metadata = store.get(request["id"])["metadata"]
    assert metadata["episode_ids"] == [102, 201] and metadata["command_ids"] == [1]
    assert not metadata.get("baseline_episode_files"), "episode 102 held no file"
    # An import is tracked from its importing phase, for the movie it is for.
    imported = tk.call("manual_import", {"kind": "movie", "download_id": "ABC123"})
    assert imported["ok"] and store.get(imported["operation_id"])["progress"] == {
        "phase": "importing"
    }
    rows, total = store.for_assistant("active", limit=2)
    assert total == 3 and [r["title"] for r in rows] == ["Dune", "Breaking Bad"]
    assert [
        r["title"] for r in store.for_assistant("active", limit=2, offset=2)[0]
    ] == ["Alien"]
    # A grab of a series release without its season cannot be scoped.
    assert (
        "season"
        in tk.call(
            "grab_release",
            {"kind": "series", "catalog_id": 81189, "guid": "g1", "indexer_id": 3},
        )["error"]
    )


def test_one_episode_is_done_when_that_episode_arrives(stack):
    # Grabbing an upgrade for one episode of a two-episode season must not
    # wait on the other episode, and a special (season 0) is grabbable.
    svc, _, sonarr, _ = stack
    sonarr.answers["episode"] = lambda p: [
        {
            "id": 101,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "hasFile": True,
            "episodeFileId": 1001,
            "monitored": True,
            "airDateUtc": iso(-400),
        },
        {
            "id": 102,
            "seasonNumber": 1,
            "episodeNumber": 2,
            "hasFile": True,
            "episodeFileId": 1002,
            "monitored": True,
            "airDateUtc": iso(-390),
        },
        {
            "id": 900,
            "seasonNumber": 0,
            "episodeNumber": 1,
            "hasFile": False,
            "monitored": False,
            "airDateUtc": iso(-10),
        },
    ]
    sub = svc.grab_release("series", 81189, "g1", 3, season=1, episode=1)
    assert sub["episode_ids"] == [101] and "seasons" not in sub
    assert sub["baseline_episode_files"] == {"101": 1001}
    assert sonarr.posts[-1] == ("release", {"guid": "g1", "indexerId": 3})
    out = svc.observe_series(
        5, baseline_episode_files=sub["baseline_episode_files"], episode_ids=[101]
    )
    assert not out["complete"] and out["progress"]["total_episodes"] == 1
    sonarr.answers["episode"]({})[0]["episodeFileId"]  # the rows are rebuilt each call
    rows = sonarr.answers["episode"]({})
    rows[0]["episodeFileId"] = 1010
    sonarr.answers["episode"] = lambda p: rows
    out = svc.observe_series(
        5, baseline_episode_files=sub["baseline_episode_files"], episode_ids=[101]
    )
    assert out["complete"] and out["progress"]["episodes"] == 1
    # Episode 2 kept its old file and was never in scope.
    # A season pack covers the season's episodes, specials included when it
    # is season 0; an unmonitored special counts because it was chosen.
    special = svc.grab_release("series", 81189, "g2", 3, season=0)
    assert special["episode_ids"] == [900]
    out = svc.observe_series(5, episode_ids=[900])
    assert not out["complete"] and out["progress"]["total_episodes"] == 1
    rows[2].update(hasFile=True, episodeFileId=9000)
    assert svc.observe_series(5, episode_ids=[900])["complete"]
    # A season search for the specials is a season search, seasonNumber 0.
    sub = svc.search_again("series", 81189, season=0)
    assert sonarr.posts[-1][1] == {
        "name": "SeasonSearch",
        "seriesId": 5,
        "seasonNumber": 0,
    }
    assert sub["episode_ids"] == [900] and sub["promise"] == "search"


def test_a_search_promises_the_search_not_the_old_file(stack):
    svc, radarr, _, _ = stack
    log = CapturingLog("voice")
    store = operations.OperationStore(log)
    # Dune already has a file: a fresh search must not report it done at
    # once, and must end when the search has run, whatever it found.
    sub = svc.search_again("movie", 438631)
    assert sub["promise"] == "search" and sub["baseline_file_id"] == 501
    assert sub["command_ids"] == [99]
    radarr.answers["command/99"] = {"id": 99, "status": "started"}
    op = operations.track(store, sub)
    seen = svc.observe(store.get(op["operation_id"]))
    assert not seen["complete"] and seen["progress"]["phase"] == "searching"
    radarr.answers["command/99"] = {"id": 99, "status": "completed"}
    seen = svc.observe(store.get(op["operation_id"]))
    assert seen["complete"] and seen["progress"]["phase"] == "searched"
    done = store.observe(op["operation_id"], operations.SUCCEEDED, seen["progress"], "")
    assert done["summary"] == "Radarr searched again for Dune and found nothing better."
    # A search that found a better file is the file arriving.
    radarr.answers["moviefile"] = lambda p: [{"id": 777}]
    assert svc.observe_movie(1, 501, [99], "searching", promise="search")["detail"] == (
        "Radarr imported the requested movie upgrade"
    )
    # A search the app failed is a failure, not a success.
    radarr.answers["moviefile"] = lambda p: [{"id": 501}]
    radarr.answers["command/99"] = {"id": 99, "status": "failed"}
    failed = svc.observe_movie(1, 501, [99], "searching", promise="search")
    assert failed["complete"] and failed["failed"]
    # The first operation is closed; the failure lands on a fresh one.
    again = operations.track(store, sub)
    assert again["operation_id"] != op["operation_id"]
    row = store.observe(
        again["operation_id"], operations.FAILED, failed["progress"], ""
    )
    assert row["summary"] == "Radarr's search for Dune failed."
