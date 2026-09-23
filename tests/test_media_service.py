"""MediaService: catalog, library, requests, observation, cancel and abandon."""

import datetime

import pytest

from helpers import SERVICE_CFG, CapturingLog, FakeArr, sonarr_episode
from slopstation.agent import media, operations
from slopstation.agent.media import clients

UTC = datetime.UTC


@pytest.fixture
def svc():
    """A MediaService over two fresh Arrs whose profiles match the config, so
    a request has everything it needs but the title."""
    radarr = FakeArr(
        "Radarr",
        profiles=[{"id": 10, "name": "Movie UHD"}, {"id": 11, "name": "Movie HD"}],
    )
    sonarr = FakeArr(
        "Sonarr",
        profiles=[{"id": 20, "name": "Series HD"}, {"id": 21, "name": "Series UHD"}],
    )
    return media.MediaService(SERVICE_CFG, CapturingLog("voice"), radarr, sonarr)


# --- catalog lookups ----------------------------------------------------------


def test_find_trims_and_caps_results(svc):
    svc.radarr.set(
        lookup=[
            {
                "tmdbId": n,
                "title": f"Movie {n}\n",
                "year": 2000 + n,
                "status": "released",
            }
            for n in range(1, 8)
        ]
    )
    found = svc.find("movie", "Movie")
    assert len(found) == 5 and found[0] == {
        "tmdb_id": 1,
        "title": "Movie 1",
        "year": 2001,
        "status": "released",
    }


# --- library ------------------------------------------------------------------


def test_library_names_an_id_it_does_not_hold(svc):
    """A wrong id is caught here rather than after a request has added it."""
    svc.sonarr.set(lookup=[{"tvdbId": 403180, "title": "Bloomin' Marvellous"}])
    assert svc.library("series", 403180) == {
        "kind": "series",
        "catalog_id": 403180,
        "in_library": False,
        "title": "Bloomin' Marvellous",
    }


def test_library_reports_holdings_per_season(svc):
    assert svc.library("movie", 438631) == {
        "kind": "movie",
        "catalog_id": 438631,
        "in_library": False,
        "title": None,
    }
    svc.radarr.set(
        library=[{"id": 32, "tmdbId": 438631, "title": "Dune", "hasFile": True}]
    )
    held = svc.library("movie", 438631)
    assert held["in_library"] and held["available"] and held["title"] == "Dune"
    assert svc.library("series", 81189)["in_library"] is False
    svc.sonarr.set(
        library=[{"id": 41, "tvdbId": 81189, "title": "Breaking Bad"}],
        episodes=[
            sonarr_episode(
                1, 1, has_file=True, monitored=False, aired="2008-01-20T00:00:00Z"
            ),
            sonarr_episode(
                2, 1, has_file=False, monitored=False, aired="2008-01-27T00:00:00Z"
            ),
            sonarr_episode(
                3, 2, has_file=True, monitored=True, aired="2009-03-08T00:00:00Z"
            ),
            sonarr_episode(
                4, 2, has_file=False, monitored=True, aired="2999-01-01T00:00:00Z"
            ),
            sonarr_episode(
                5, 0, has_file=True, monitored=True, aired="2009-01-01T00:00:00Z"
            ),
        ],
    )
    owned = svc.library("series", 81189)
    assert owned["title"] == "Breaking Bad"
    assert owned["seasons"] == [
        {"season": 1, "have": 1, "aired": 2},
        {"season": 2, "have": 1, "aired": 1},
    ]
    with pytest.raises(clients.MediaError):
        svc.library("album", 1)


# --- abandon ------------------------------------------------------------------


def test_abandon_missing_unmonitors_the_gap(svc):
    svc.sonarr.set(
        library=[{"id": 41, "tvdbId": 81189, "title": "Breaking Bad"}],
        episodes=[
            sonarr_episode(
                1, 1, has_file=False, monitored=True, aired="2008-01-20T00:00:00Z"
            ),
            sonarr_episode(
                2, 1, has_file=False, monitored=True, aired="2008-01-27T00:00:00Z"
            ),
            sonarr_episode(
                3, 2, has_file=True, monitored=True, aired="2009-03-08T00:00:00Z"
            ),
        ],
    )
    result = svc.abandon_missing(
        {
            "kind": "series_acquisition",
            "external_ref": "41",
            "metadata": {"seasons": None},
        }
    )
    assert result == {
        "have": 1,
        "missing": [{"season": 1, "episodes": 2}],
        "episode_ids": [1, 2],
    }
    assert svc.sonarr.puts[-1] == (
        "episode/monitor",
        {"episodeIds": [1, 2], "monitored": False},
    )
    svc.radarr.set(
        library=[
            {
                "id": 32,
                "tmdbId": 438631,
                "title": "Dune",
                "hasFile": False,
                "monitored": True,
            }
        ]
    )
    result = svc.abandon_missing({"kind": "movie_acquisition", "external_ref": "32"})
    assert result == {"have": 0, "missing": [], "episode_ids": []}
    assert svc.radarr.puts[-1][1]["monitored"] is False


def _cancel_fixture(svc):
    """A request with one episode imported, one missing and downloading, and
    two searches out: one queued, one already started."""
    svc.sonarr.set(
        library=[{"id": 5, "tvdbId": 81189, "title": "Breaking Bad"}],
        episodes=[
            {"id": 101, "seasonNumber": 1, "monitored": True, "hasFile": True},
            {"id": 102, "seasonNumber": 1, "monitored": True, "hasFile": False},
        ],
        queue={
            "records": [
                {"id": 720, "seriesId": 5, "episodeId": 102, "downloadId": "d1"},
                # Another request's episode: a cancel must leave it alone.
                {"id": 721, "seriesId": 5, "episodeId": 999, "downloadId": "d2"},
            ]
        },
        commands={1: {"status": "queued"}, 2: {"status": "started"}},
    )
    for row in svc.sonarr.episodes:
        row["airDateUtc"] = "2008-01-20T00:00:00Z"
    return {
        "kind": "series_acquisition",
        "external_ref": "5",
        "metadata": {"catalog_id": 81189, "seasons": [1], "command_ids": [1, 2]},
    }


def test_cancel_stops_the_search_and_keeps_what_imported(svc, monkeypatch):
    """Unmonitor the missing episode, recall only the search that has not
    started, drop only that scope's download - in that order, so the running
    search finds nothing left to grab."""
    operation = _cancel_fixture(svc)
    assert svc.cancel_targets(operation) == {"have": 1, "missing": 1, "downloads": 1}
    calls = []
    for verb in ("put", "delete"):
        original = getattr(svc.sonarr, verb)

        def traced(endpoint, payload=None, _verb=verb, _original=original):
            calls.append((_verb, endpoint))
            return _original(endpoint, payload)

        monkeypatch.setattr(svc.sonarr, verb, traced)
    assert svc.cancel_request(operation) == {
        "ok": True,
        "kind": "series",
        "have": 1,
        "unmonitored": 1,
        "downloads_canceled": 1,
        "searches_canceled": 1,
        "searches_running": 1,
    }
    assert calls == [
        ("put", "episode/monitor"),
        ("delete", "command/1"),
        ("delete", "queue/720"),
    ]
    assert svc.sonarr.puts[-1][1] == {"episodeIds": [102], "monitored": False}
    assert svc.sonarr.episodes[0]["monitored"] and svc.sonarr.episodes[0]["hasFile"]


def test_cancel_counts_a_search_that_starts_before_the_recall_lands(svc, monkeypatch):
    """409 means it started between the read and the delete. Anything else
    is a real failure."""
    operation = _cancel_fixture(svc)
    monkeypatch.setitem(svc.sonarr.commands, 2, {"status": "queued"})

    def refuse(endpoint, params=None):
        if endpoint == "command/2":
            raise clients.MediaError("returned HTTP 409 for /api/v3/command/2")
        svc.sonarr.deletes.append((endpoint, params))

    monkeypatch.setattr(svc.sonarr, "delete", refuse)
    result = svc.cancel_request(operation)
    assert result["searches_canceled"] == 1 and result["searches_running"] == 1

    def fail(endpoint, params=None):
        raise clients.MediaError("returned HTTP 500 for /api/v3/command/1")

    monkeypatch.setattr(svc.sonarr, "delete", fail)
    with pytest.raises(clients.MediaError, match="HTTP 500"):
        svc.cancel_request(operation)


def test_cancel_of_a_pending_episode_request_leaves_the_series_alone(svc):
    """A request whose episode ids Sonarr has not named yet carries its scope
    as the requested pairs alone. Read as no scope at all, a cancel would take
    every monitored episode of the series."""
    svc.sonarr.set(
        library=[{"id": 5, "tvdbId": 81189, "title": "Breaking Bad"}],
        # Somebody else's work, downloading right now.
        episodes=[
            sonarr_episode(
                201,
                2,
                number=1,
                monitored=True,
                has_file=False,
                aired="2009-03-08T00:00:00Z",
            )
        ],
        queue={
            "records": [
                {"id": 730, "seriesId": 5, "episodeId": 201, "downloadId": "d9"}
            ]
        },
    )
    pending = {
        "kind": "series_acquisition",
        "external_ref": "5",
        "metadata": {"catalog_id": 81189, "episodes": [[4, 13]]},
    }
    assert svc.cancel_targets(pending) == {"have": 0, "missing": 0, "downloads": 0}
    assert svc.cancel_request(pending)["downloads_canceled"] == 0
    assert svc.sonarr.puts == [] and svc.sonarr.deletes == []

    # Once Sonarr names the episode, the cancel acts on that one and no other.
    svc.sonarr.episodes.append(
        sonarr_episode(
            413,
            4,
            number=13,
            monitored=True,
            has_file=False,
            aired="2008-11-20T00:00:00Z",
        )
    )
    svc.sonarr.queue["records"].append(
        {"id": 731, "seriesId": 5, "episodeId": 413, "downloadId": "d10"}
    )
    assert svc.cancel_request(pending)["downloads_canceled"] == 1
    assert svc.sonarr.puts == [
        ("episode/monitor", {"episodeIds": [413], "monitored": False})
    ]
    assert [endpoint for endpoint, _ in svc.sonarr.deletes] == ["queue/731"]
    assert svc.sonarr.episodes[0]["monitored"]


# --- movies -------------------------------------------------------------------


def test_request_movie_adds_and_searches(svc):
    svc.radarr.set(
        lookup_by_id={"tmdbId": 438631, "title": "Dune", "year": 2021},
        created={"id": 31, "tmdbId": 438631, "title": "Dune", "hasFile": False},
    )
    submitted = svc.request_movie(438631)
    endpoint, payload = svc.radarr.posts[0]
    assert endpoint == "movie" and payload["rootFolderPath"] == "/data/Movies"
    assert payload["qualityProfileId"] == 10
    assert payload["addOptions"] == {"searchForMovie": False, "addMethod": "manual"}
    assert submitted["external_ref"] == "31" and not submitted["already_available"]
    assert submitted["command_ids"] == [1]


def test_request_movie_changes_the_profile_and_skips_what_is_held(svc):
    svc.radarr.set(
        library=[
            {
                "id": 32,
                "tmdbId": 438631,
                "title": "Dune",
                "qualityProfileId": 10,
                "hasFile": False,
            }
        ]
    )
    changed = svc.request_movie(438631, "1080p")
    assert changed["profile"] == "Movie HD"
    assert svc.radarr.puts[0][1]["qualityProfileId"] == 11
    assert svc.radarr.posts[-1] == (
        "command",
        {"name": "MoviesSearch", "movieIds": [32]},
    )
    svc.radarr.library[0]["hasFile"] = True
    before = len(svc.radarr.posts)
    ready = svc.request_movie(438631, "1080p")
    assert ready["already_available"] and len(svc.radarr.posts) == before


def test_movie_upgrade_completes_on_a_new_file(svc):
    svc.radarr.set(
        library=[
            {
                "id": 33,
                "tmdbId": 438631,
                "title": "Dune",
                "qualityProfileId": 11,
                "hasFile": True,
            }
        ],
        movie_files=[{"id": 71, "movieId": 33}],
    )
    upgrade = svc.request_movie(438631, "2160p")
    assert not upgrade["already_available"]
    assert upgrade["baseline_file_id"] == 71
    assert svc.radarr.puts[0][1]["qualityProfileId"] == 10
    operation = {
        "kind": "movie_acquisition",
        "external_ref": "33",
        "metadata": {"baseline_file_id": 71},
    }
    assert svc.observe(operation).state != operations.SUCCEEDED
    svc.radarr.movie_files[0]["id"] = 72
    assert svc.observe(operation).state == operations.SUCCEEDED


# --- selected series seasons --------------------------------------------------


def test_request_series_monitors_only_the_asked_seasons(svc):
    svc.sonarr.set(
        lookup=[
            {
                "tvdbId": 81189,
                "title": "Breaking Bad",
                "year": 2008,
                "seasons": [
                    {"seasonNumber": 0, "monitored": False},
                    {"seasonNumber": 1, "monitored": False},
                    {"seasonNumber": 2, "monitored": False},
                ],
            }
        ]
    )
    svc.sonarr.set(
        created={
            "id": 41,
            "tvdbId": 81189,
            "title": "Breaking Bad",
            "qualityProfileId": 20,
            "seasons": svc.sonarr.lookup[0]["seasons"],
        }
    )
    series = svc.request_series(81189, "default", [2])
    added = svc.sonarr.posts[0][1]
    assert added["rootFolderPath"] == "/data/TV"
    assert added["addOptions"]["monitor"] == "none"
    # Sonarr is still adding the series, so nothing is written to it yet.
    assert svc.sonarr.puts == []
    assert not [post for post in svc.sonarr.posts if post[0] == "command"]
    svc.sonarr.set(library=[dict(svc.sonarr.created)])
    assert series["seasons"] == [2] and series["search_pending"]
    pending = {
        "kind": "series_acquisition",
        "external_ref": "41",
        "metadata": {"seasons": [2], "search_pending": True},
    }
    assert not svc.dispatch_pending_series_search(pending)
    svc.sonarr.set(
        episodes=[
            sonarr_episode(
                101, 0, monitored=False, has_file=False, aired="2019-01-01T00:00:00Z"
            ),
            sonarr_episode(
                102, 2, monitored=False, has_file=False, aired="2020-01-01T00:00:00Z"
            ),
            sonarr_episode(
                103, 2, monitored=True, has_file=False, aired="2020-01-08T00:00:00Z"
            ),
        ]
    )
    assert svc.dispatch_pending_series_search(pending)
    monitored = {
        r["seasonNumber"]: r["monitored"] for r in svc.sonarr.puts[0][1]["seasons"]
    }
    assert monitored == {0: False, 1: False, 2: True}
    assert svc.sonarr.puts[0][1]["monitored"]
    assert svc.sonarr.puts[-1] == (
        "episode/monitor",
        {"episodeIds": [102], "monitored": True},
    )
    assert svc.sonarr.posts[-1] == (
        "command",
        {"name": "SeasonSearch", "seriesId": 41, "seasonNumber": 2},
    )
    observation = svc.observe_series(41, [2])
    assert observation.progress == {
        "episodes": 0,
        "total_episodes": 2,
        "percent": 0,
        "phase": "waiting_for_match",
    }
    with pytest.raises(clients.MediaError):
        svc.request_series(81189, seasons=[0])

    # A retry after the season search above is one SeasonSearch per season.
    series_retry = {
        "kind": "series_acquisition",
        "external_ref": "41",
        "metadata": {"seasons": [1, 2]},
    }
    assert svc.retry_search(series_retry) == [2, 3]
    assert svc.sonarr.posts[-2:] == [
        ("command", {"name": "SeasonSearch", "seriesId": 41, "seasonNumber": 1}),
        ("command", {"name": "SeasonSearch", "seriesId": 41, "seasonNumber": 2}),
    ]


def test_series_monitoring_waits_for_sonarr_to_finish_adding(svc):
    """Sonarr rewrites the series while it is still acting on the add options,
    so the monitored state is written only once it has cleared them."""
    svc.sonarr.set(
        library=[
            {
                "id": 41,
                "tvdbId": 81189,
                "title": "Breaking Bad",
                "monitored": False,
                "addOptions": {"monitor": "none"},
                "seasons": [{"seasonNumber": 1, "monitored": False}],
            }
        ],
        episodes=[
            sonarr_episode(
                102, 1, monitored=False, has_file=False, aired="2020-01-01T00:00:00Z"
            )
        ],
    )
    pending = {
        "kind": "series_acquisition",
        "external_ref": "41",
        "metadata": {"seasons": [1], "search_pending": True},
    }
    assert not svc.dispatch_pending_series_search(pending)
    assert svc.sonarr.puts == []

    svc.sonarr.library[0]["addOptions"] = None
    assert svc.dispatch_pending_series_search(pending)
    written = svc.sonarr.puts[0][1]
    assert written["monitored"]
    assert [r["monitored"] for r in written["seasons"]] == [True]


def test_search_retry_waits_for_a_search_capable_indexer(svc):
    movie_retry = {"kind": "movie_acquisition", "external_ref": "31", "metadata": {}}
    assert svc.search_available(movie_retry)
    svc.radarr.set(health=[{"source": "IndexerSearchCheck"}])
    assert not svc.search_available(movie_retry)
    svc.radarr.set(health=[{"source": "IndexerRssCheck"}])
    assert svc.search_available(movie_retry), "RSS-only warning blocked a search retry"
    svc.radarr.set(health=[{"source": "IndexerStatusCheck"}])
    assert svc.search_available(movie_retry), "general status warning blocked recovery"
    svc.radarr.indexers[0]["enableAutomaticSearch"] = False
    assert not svc.search_available(movie_retry)
    svc.radarr.indexers[0]["enableAutomaticSearch"] = True
    svc.radarr.set(health=[])
    assert svc.retry_search(movie_retry) == [1]
    assert svc.radarr.posts[-1] == (
        "command",
        {"name": "MoviesSearch", "movieIds": [31]},
    )


def test_series_upgrade_completes_on_new_episode_files(svc):
    svc.sonarr.set(
        library=[
            {
                "id": 42,
                "tvdbId": 81189,
                "title": "Breaking Bad",
                "qualityProfileId": 20,
                "seasons": [
                    {"seasonNumber": 1, "monitored": False},
                    {"seasonNumber": 2, "monitored": True},
                ],
            }
        ],
        episodes=[
            sonarr_episode(
                101,
                1,
                file_id=201,
                monitored=True,
                has_file=True,
                aired="2020-01-01T00:00:00Z",
            ),
            sonarr_episode(
                102,
                1,
                file_id=0,
                monitored=True,
                has_file=False,
                aired="2020-01-08T00:00:00Z",
            ),
        ],
    )
    series_upgrade = svc.request_series(81189, "2160p", [1])
    assert series_upgrade["baseline_episode_files"] == {"101": 201}
    # Season 2 is somebody else's desired state and stays monitored.
    monitored = {
        r["seasonNumber"]: r["monitored"] for r in svc.sonarr.puts[0][1]["seasons"]
    }
    assert monitored == {1: True, 2: True}
    upgrade_operation = {
        "kind": "series_acquisition",
        "external_ref": "42",
        "metadata": {"seasons": [1], "baseline_episode_files": {"101": 201}},
    }
    assert svc.observe(upgrade_operation).state != operations.SUCCEEDED
    svc.sonarr.episodes[0]["episodeFileId"] = 301
    svc.sonarr.episodes[1].update(hasFile=True, episodeFileId=302)
    assert svc.observe(upgrade_operation).state == operations.SUCCEEDED


def test_request_episodes_touches_only_those_episodes(svc):
    """A held series asked for by episode: the named episodes are monitored
    and searched one by one; no season flag moves and no SeasonSearch runs."""
    svc.sonarr.set(
        library=[
            {
                "id": 42,
                "tvdbId": 75805,
                "title": "It's Always Sunny in Philadelphia",
                "qualityProfileId": 20,
                "monitored": False,
                "seasons": [
                    {"seasonNumber": 4, "monitored": False},
                    {"seasonNumber": 10, "monitored": False},
                ],
            }
        ],
        episodes=[
            sonarr_episode(
                413,
                4,
                number=13,
                monitored=False,
                has_file=False,
                aired="2008-11-20T00:00:00Z",
            ),
            sonarr_episode(
                412,
                4,
                number=12,
                monitored=False,
                has_file=False,
                aired="2008-11-13T00:00:00Z",
            ),
            sonarr_episode(
                1004,
                10,
                number=4,
                monitored=False,
                has_file=False,
                aired="2015-02-04T00:00:00Z",
            ),
        ],
    )
    submission = svc.request_series(
        75805, episodes=[{"season": 10, "episode": 4}, {"season": 4, "episode": 13}]
    )
    assert submission["episode_ids"] == [413, 1004]
    assert submission["episodes"] == [[4, 13], [10, 4]]
    assert "seasons" not in submission
    assert submission["scope_label"] == "episodes S04E13, S10E04"
    written = svc.sonarr.puts[0][1]
    assert written["monitored"]
    assert [r["monitored"] for r in written["seasons"]] == [False, False]
    assert svc.sonarr.puts[1] == (
        "episode/monitor",
        {"episodeIds": [413, 1004], "monitored": True},
    )
    assert svc.sonarr.posts == [
        ("command", {"name": "EpisodeSearch", "episodeIds": [413, 1004]})
    ]
    operation = {
        "kind": "series_acquisition",
        "external_ref": "42",
        "metadata": {"episodes": [[4, 13], [10, 4]], "episode_ids": [413, 1004]},
    }
    observation = svc.observe(operation)
    assert observation.progress["total_episodes"] == 2
    assert observation.state != operations.SUCCEEDED
    with pytest.raises(clients.MediaError, match="S04E99"):
        svc.request_series(75805, episodes=[{"season": 4, "episode": 99}])
    with pytest.raises(clients.MediaError, match="not both"):
        svc.request_series(75805, seasons=[4], episodes=[{"season": 4, "episode": 13}])


def test_request_episodes_on_a_new_series_resolves_ids_when_sonarr_is_ready(svc):
    """A series Sonarr is still adding has no episode rows to name, so the
    request waits, and the pending dispatch resolves the ids, monitors the
    episodes and searches them once the rows and the add pass are there."""
    svc.sonarr.set(
        lookup=[
            {
                "tvdbId": 75805,
                "title": "It's Always Sunny in Philadelphia",
                "seasons": [{"seasonNumber": 4, "monitored": False}],
            }
        ]
    )
    svc.sonarr.set(
        created={
            "id": 43,
            "tvdbId": 75805,
            "title": "It's Always Sunny in Philadelphia",
            "qualityProfileId": 20,
            "addOptions": {"monitor": "none"},
            "seasons": svc.sonarr.lookup[0]["seasons"],
        }
    )
    submission = svc.request_series(75805, episodes=[{"season": 4, "episode": 13}])
    assert svc.sonarr.posts[0][1]["addOptions"]["monitor"] == "none"
    assert submission["search_pending"] and "episode_ids" not in submission
    assert submission["episodes"] == [[4, 13]]
    svc.sonarr.set(library=[dict(svc.sonarr.created)])
    pending = {
        "kind": "series_acquisition",
        "external_ref": "43",
        "metadata": {"episodes": [[4, 13]], "search_pending": True},
    }
    # No rows yet: not ready, and not cancelled either.
    assert not svc.dispatch_pending_series_search(pending)
    assert not svc.observe(pending).metadata_ready
    svc.sonarr.set(
        episodes=[
            sonarr_episode(
                413,
                4,
                number=13,
                monitored=False,
                has_file=False,
                aired="2008-11-20T00:00:00Z",
            )
        ]
    )
    # Rows, but Sonarr's add pass is still running.
    assert not svc.dispatch_pending_series_search(pending)
    assert svc.observe(pending).state != operations.CANCELED
    svc.sonarr.library[0]["addOptions"] = None
    assert svc.dispatch_pending_series_search(pending) == {
        "command_ids": [1],
        "episode_ids": [413],
    }
    assert svc.sonarr.puts[0][1]["monitored"]
    assert svc.sonarr.puts[-1] == (
        "episode/monitor",
        {"episodeIds": [413], "monitored": True},
    )
    assert svc.sonarr.posts[-1] == (
        "command",
        {"name": "EpisodeSearch", "episodeIds": [413]},
    )
    # The scope an abandonment reads back, so a pending request is never
    # mistaken for one with no scope at all.
    assert svc.episodes_in_scope(75805, [[4, 13]]) == [413]
    assert svc.episodes_in_scope(75805, [[4, 99]]) == []


# --- positive completion evidence ---------------------------------------------


def test_observe_movie_reports_the_download_without_its_title(svc):
    svc.radarr.set(
        library=[{"id": 50, "tmdbId": 1, "title": "Arrival", "hasFile": False}],
        queue={
            "records": [
                {
                    "movieId": 50,
                    "size": 1000,
                    "sizeleft": 250,
                    "title": "UNTRUSTED RELEASE TEXT",
                }
            ]
        },
    )
    movie_progress = svc.observe_movie(50)
    assert movie_progress.state != operations.SUCCEEDED
    assert movie_progress.progress == {"phase": "downloading", "percent": 75}
    assert "UNTRUSTED" not in movie_progress.detail
    svc.radarr.library[0]["hasFile"] = True
    assert svc.observe_movie(50).state == operations.SUCCEEDED


def test_observe_series_counts_aired_monitored_episodes(svc):
    now = datetime.datetime(2026, 8, 29, tzinfo=UTC)
    empty = svc.observe_series(60, [1], now)
    assert not empty.metadata_ready
    assert empty.detail == "Sonarr is still populating episode metadata"
    svc.sonarr.set(
        episodes=[
            {
                "seasonNumber": 0,
                "monitored": True,
                "hasFile": False,
                "airDateUtc": "2020-01-01T00:00:00Z",
            },
            {
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": True,
                "airDateUtc": "2020-01-01T00:00:00Z",
            },
            {
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": False,
                "airDateUtc": "2020-01-08T00:00:00Z",
            },
            {
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": False,
                "airDateUtc": "2027-01-01T00:00:00Z",
            },
            {
                "seasonNumber": 2,
                "monitored": False,
                "hasFile": False,
                "airDateUtc": "2020-01-01T00:00:00Z",
            },
        ]
    )
    progress = svc.observe_series(60, None, now)
    assert progress.metadata_ready
    assert progress.state != operations.SUCCEEDED
    assert progress.progress == {
        "episodes": 1,
        "total_episodes": 2,
        "percent": 50,
        "phase": "waiting_for_match",
    }
    svc.sonarr.episodes[2]["hasFile"] = True
    assert svc.observe_series(60, None, now).state == operations.SUCCEEDED
    for episode in svc.sonarr.episodes:
        if episode["seasonNumber"] == 1:
            episode["monitored"] = False
    canceled = svc.observe_series(60, [1], now)
    assert canceled.state == operations.CANCELED


# --- Abandoned requests -------------------------------------------------------


def test_delete_movie_cancels_its_downloads(svc):
    svc.radarr.set(
        library=[
            {
                "id": 70,
                "tmdbId": 438631,
                "title": "Dune",
                "monitored": True,
                "hasFile": True,
            }
        ],
        queue={
            "records": [
                {"id": 700, "movieId": 70, "downloadId": "same", "size": 100},
                {"id": 701, "movieId": 70, "downloadId": "same", "size": 100},
            ]
        },
    )
    svc.radarr.commands[8] = {"id": 8, "status": "started"}
    svc.radarr.commands[9] = {"id": 9, "status": "queued"}
    removed_movie = svc.delete_movie(438631, [8, 9])
    assert removed_movie["downloads_canceled"] == 1
    assert removed_movie["files_deleted"] == 1
    assert svc.radarr.puts[0][1]["monitored"] is False
    # The two queue rows are one download and one removal. Command 8 has
    # started, which the app refuses to cancel, so only 9 is recalled -
    # deleting the movie is what stops 8 from grabbing anything.
    assert svc.radarr.deletes == [
        ("command/9", None),
        (
            "queue/700",
            {
                "removeFromClient": True,
                "blocklist": False,
                "skipRedownload": True,
                "changeCategory": False,
            },
        ),
        ("movie/70", {"deleteFiles": True, "addImportExclusion": False}),
    ]


def test_delete_series_is_scoped_to_seasons(svc):
    svc.sonarr.set(
        library=[
            {
                "id": 71,
                "tvdbId": 393189,
                "title": "Andor",
                "monitored": True,
                "seasons": [
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": True},
                ],
            }
        ],
        episodes=[
            {
                "id": 710,
                "seriesId": 71,
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": True,
                "episodeFileId": 810,
            },
            {
                "id": 711,
                "seriesId": 71,
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": False,
                "episodeFileId": 0,
            },
            {
                "id": 712,
                "seriesId": 71,
                "seasonNumber": 2,
                "monitored": True,
                "hasFile": True,
                "episodeFileId": 812,
            },
        ],
        queue={
            "records": [
                {
                    "id": 720,
                    "seriesId": 71,
                    "episodeId": 710,
                    "downloadId": "season-one",
                },
                {
                    "id": 721,
                    "seriesId": 71,
                    "episodeId": 711,
                    "downloadId": "season-one",
                },
                {
                    "id": 722,
                    "seriesId": 71,
                    "episodeId": 712,
                    "downloadId": "season-two",
                },
            ]
        },
    )
    removed_season = svc.delete_series(393189, seasons=[1])
    assert removed_season["downloads_canceled"] == 1
    assert removed_season["files_deleted"] == 1
    assert svc.sonarr.puts[0] == (
        "episode/monitor",
        {"episodeIds": [710, 711], "monitored": False},
    )
    monitored = {
        r["seasonNumber"]: r["monitored"] for r in svc.sonarr.puts[1][1]["seasons"]
    }
    assert monitored == {1: False, 2: True}
    assert [endpoint for endpoint, _ in svc.sonarr.deletes] == [
        "queue/720",
        "episodefile/810",
    ]
    with pytest.raises(clients.MediaError):
        svc.delete_series(393189)
    removed_all = svc.delete_series(393189, all_seasons=True)
    assert removed_all["all_seasons"]
    assert svc.sonarr.deletes[-1] == (
        "series/71",
        {"deleteFiles": True, "addImportListExclusion": False},
    )
