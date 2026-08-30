"""Blind test: Radarr/Sonarr API boundary, policy, and completion evidence."""
import datetime
import json
import tempfile
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
        self.health = []
        self.indexers = [{"id": 1, "enable": True,
                          "enableAutomaticSearch": True}]
        self.status_row = {"version": "1.2.3", "appName": name}
        self.posts = []
        self.puts = []
        self.deletes = []
        self.commands = {}
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
        if endpoint.startswith("command/"):
            command_id = int(endpoint.split("/")[1])
            return dict(self.commands.get(command_id, {
                "id": command_id, "status": "completed", "result": "successful"}))
        if endpoint == "system/status":
            return dict(self.status_row)
        if endpoint == "rootfolder":
            return [dict(row) for row in self.root_folders]
        if endpoint == "health":
            return [dict(row) for row in self.health]
        if endpoint == "indexer":
            return [dict(row) for row in self.indexers]
        raise AssertionError((self.name, "GET", endpoint, params))

    def post(self, endpoint, payload):
        self.posts.append((endpoint, json.loads(json.dumps(payload))))
        if endpoint in ("movie", "series"):
            return dict(self.created)
        if endpoint == "command":
            command_id = len(self.commands) + 1
            self.commands[command_id] = {
                "id": command_id, "status": "started", "result": "unknown"}
            return dict(self.commands[command_id])
        raise AssertionError((self.name, "POST", endpoint, payload))

    def put(self, endpoint, payload):
        self.puts.append((endpoint, json.loads(json.dumps(payload))))
        if endpoint == "episode/monitor":
            episode_ids = set(payload["episodeIds"])
            for episode in self.episodes:
                if episode.get("id") in episode_ids:
                    episode["monitored"] = bool(payload["monitored"])
        elif endpoint.startswith(("movie/", "series/")):
            wanted = int(endpoint.split("/")[1])
            for index, row in enumerate(self.library):
                if int(row["id"]) == wanted:
                    self.library[index] = json.loads(json.dumps(payload))
        return dict(payload)

    def delete(self, endpoint, params=None, payload=None):
        self.deletes.append((endpoint, params, payload))
        if endpoint.startswith("command/"):
            self.commands.pop(int(endpoint.split("/")[1]), None)
        elif endpoint.startswith("queue/"):
            queue_id = int(endpoint.split("/")[1])
            selected = next(row for row in self.queue["records"]
                            if int(row["id"]) == queue_id)
            download_id = selected.get("downloadId")
            self.queue["records"] = [
                row for row in self.queue["records"]
                if (row.get("downloadId") != download_id if download_id
                    else int(row.get("id", 0)) != queue_id)]
        elif endpoint.startswith("movie/"):
            wanted = int(endpoint.split("/")[1])
            self.library = [row for row in self.library if row["id"] != wanted]
        elif endpoint.startswith("series/"):
            wanted = int(endpoint.split("/")[1])
            self.library = [row for row in self.library if row["id"] != wanted]
        elif endpoint.startswith("episodefile/"):
            wanted = int(endpoint.split("/")[1])
            for episode in self.episodes:
                if int(episode.get("episodeFileId", 0) or 0) == wanted:
                    episode.update(hasFile=False, episodeFileId=0)
        return None


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

    qbit_calls = []
    qbit_preferences = {"listen_port": 6881}

    def qbit_transport(method, url, headers, body, timeout):
        qbit_calls.append((method, url, headers, body, timeout))
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/auth/login"):
            return {"Set-Cookie": "QBT_SID_8080=session-1; HttpOnly; path=/"}, b""
        if headers.get("Cookie") == "QBT_SID_8080=expired":
            raise media.QbittorrentAuthError("expired session")
        assert headers["Cookie"] == "QBT_SID_8080=session-1"
        if path.endswith("/app/preferences"):
            return {}, json.dumps(qbit_preferences).encode()
        if path.endswith("/app/setPreferences"):
            changes = json.loads(urllib.parse.parse_qs(
                body.decode())["json"][0])
            qbit_preferences.update(changes)
            return {}, b""
        raise AssertionError((method, path))

    qbit = media.QbittorrentClient(
        "http://127.0.0.1:8080", "admin", "a-long-qbit-password",
        transport=qbit_transport)
    changed_port = qbit.set_listen_port(33125)
    assert changed_port == {"ok": True, "previous_port": 6881,
                            "listen_port": 33125, "changed": True}
    assert qbit_calls[0][2]["Origin"] == "http://127.0.0.1:8080"
    assert urllib.parse.parse_qs(qbit_calls[0][3].decode()) == {
        "username": ["admin"], "password": ["a-long-qbit-password"]}
    assert len([row for row in qbit_calls
                if row[1].endswith("/app/setPreferences")]) == 1
    qbit.set_listen_port(33125)
    assert len([row for row in qbit_calls
                if row[1].endswith("/app/setPreferences")]) == 1
    try:
        qbit.set_listen_port(0)
        raise AssertionError("invalid qBittorrent port accepted")
    except media.MediaError:
        pass
    qbit.sid = "expired"
    assert qbit.preferences()["listen_port"] == 33125
    assert len([row for row in qbit_calls
                if row[1].endswith("/auth/login")]) == 2
    print("  qBittorrent: cookie auth, explicit port mutation, read-back verification")

    proton_dir = Path(tempfile.mkdtemp(prefix="cg-proton-log-"))
    proton_log = proton_dir / "client-logs.txt"

    def proton_event(timestamp, status, port=None):
        pair = "" if port is None else f", Port pair {port}->{port}, expiring in 00:01:00"
        return (f"{timestamp} | INFO  | PROCESS.COMM | Received PortForwarding "
                f"Status '{status}' triggered at 'fixture'{pair} |\n"
                "{\"Caller\":\"ClientControllerListener\"}\n")

    proton_log.write_text(
        proton_event("2026-08-27T18:13:17.939Z", "Stopped")
        + proton_event("2026-08-30T04:10:26.030Z", "Starting")
        + proton_event("2026-08-30T04:10:26.031Z", "HelloCommunication")
        + proton_event("2026-08-30T04:10:26.047Z", "PortMappingCommunication")
        + proton_event("2026-08-30T04:10:36.034Z", "SleepingUntilRefresh", 39733),
        encoding="utf-8")
    proton_now = datetime.datetime(2026, 8, 30, 4, 10, 40,
                                   tzinfo=datetime.timezone.utc)
    source = media.read_proton_port_state(proton_log, now=proton_now)
    assert source["state"] == "active" and source["port"] == 39733
    qbit_preferences["listen_port"] = 33125
    proton_monitor = media.ProtonPortMonitor(
        qbit, cglib.CapturingLog("voice"), path=proton_log, now=proton_now)
    synced = proton_monitor.reconcile_once()
    assert synced["changed"] and synced["previous_port"] == 33125
    assert synced["listen_port"] == 39733
    mutations = len([row for row in qbit_calls
                     if row[1].endswith("/app/setPreferences")])
    assert not proton_monitor.reconcile_once()["changed"]
    assert len([row for row in qbit_calls
                if row[1].endswith("/app/setPreferences")]) == mutations

    proton_log.write_text(
        proton_log.read_text(encoding="utf-8")
        + proton_event("2026-08-30T04:10:41.000Z",
                       "DestroyPortMappingCommunication", 39733)
        + proton_event("2026-08-30T04:10:41.100Z", "Stopped"),
        encoding="utf-8")
    proton_monitor.now = datetime.datetime(
        2026, 8, 30, 4, 10, 42, tzinfo=datetime.timezone.utc)
    assert proton_monitor.reconcile_once()["state"] == "inactive"
    proton_monitor.now = datetime.datetime(
        2026, 8, 30, 4, 12, 0, tzinfo=datetime.timezone.utc)
    assert proton_monitor.inspect()["state"] == "stale"
    try:
        proton_monitor.reconcile_once()
        raise AssertionError("stale Proton state was accepted")
    except media.MediaError:
        pass
    proton_log.write_text(
        proton_event("2026-08-30T04:12:01.000Z", "Starting"),
        encoding="utf-8")
    proton_monitor.now = datetime.datetime(
        2026, 8, 30, 4, 12, 2, tzinfo=datetime.timezone.utc)
    assert proton_monitor.reconcile_once()["state"] == "transitional"
    assert media.read_proton_port_state(
        proton_dir / "missing.txt", now=proton_now)["state"] == "missing"
    proton_log.write_text("not a Proton status line", encoding="utf-8")
    assert media.read_proton_port_state(
        proton_log, now=proton_now)["state"] == "unknown"
    proton_backup = proton_dir / "client-logs.1.txt"
    proton_backup.write_text(
        proton_event("2026-08-30T04:12:03.000Z",
                     "SleepingUntilRefresh", 40123), encoding="utf-8")
    rotated = media.read_proton_port_state(
        proton_log, now=datetime.datetime(
            2026, 8, 30, 4, 12, 4, tzinfo=datetime.timezone.utc))
    assert rotated["state"] == "active" and rotated["port"] == 40123
    print("  Proton: reconnect mapping, no-op repeat, teardown, stale and unknown states")

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
    assert payload["addOptions"] == {"searchForMovie": False, "addMethod": "manual"}
    assert submitted["external_ref"] == "31" and not submitted["already_available"]
    assert submitted["command_ids"] == [1]

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
    ready = svc.request_movie(438631, "1080p")
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
        "episodes": 0, "total_episodes": 2, "percent": 0,
        "phase": "waiting_for_match"}
    try:
        svc.request_series(81189, seasons=[0])
        raise AssertionError("specials accepted as a normal season")
    except media.MediaError:
        pass

    movie_retry = {
        "kind": "movie_acquisition", "external_ref": "31", "metadata": {}}
    assert svc.search_available(movie_retry)
    svc.radarr.health = [{"source": "IndexerSearchCheck"}]
    assert not svc.search_available(movie_retry)
    svc.radarr.health = [{"source": "IndexerRssCheck"}]
    assert svc.search_available(movie_retry), "RSS-only warning blocked a search retry"
    svc.radarr.health = [{"source": "IndexerStatusCheck"}]
    assert svc.search_available(movie_retry), "general status warning blocked recovery"
    svc.radarr.indexers[0]["enableAutomaticSearch"] = False
    assert not svc.search_available(movie_retry)
    svc.radarr.indexers[0]["enableAutomaticSearch"] = True
    svc.radarr.health = []
    assert svc.retry_search(movie_retry) == [1]
    assert svc.radarr.posts[-1] == (
        "command", {"name": "MoviesSearch", "movieIds": [31]})

    series_retry = {
        "kind": "series_acquisition", "external_ref": "41",
        "metadata": {"seasons": [1, 2]}}
    assert svc.retry_search(series_retry) == [2, 3]
    assert svc.sonarr.posts[-2:] == [
        ("command", {"name": "SeasonSearch", "seriesId": 41,
                     "seasonNumber": 1}),
        ("command", {"name": "SeasonSearch", "seriesId": 41,
                     "seasonNumber": 2}),
    ]
    print("  retry: aggregate search health gates scoped movie and series searches")

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
    assert movie_progress["progress"] == {"phase": "downloading", "percent": 75}
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
                                    "percent": 50,
                                    "phase": "waiting_for_match"}
    svc.sonarr.episodes[2]["hasFile"] = True
    assert svc.observe_series(60, None, now)["complete"]
    for episode in svc.sonarr.episodes:
        if episode["seasonNumber"] == 1:
            episode["monitored"] = False
    canceled = svc.observe_series(60, [1], now)
    assert canceled["canceled"] and not canceled["complete"]
    print("  observe: queue bytes ignored except percent; only aired monitored files complete")

    # --- authoritative abandonment ----------------------------------------
    svc = service()
    svc.radarr.library = [{"id": 70, "tmdbId": 438631, "title": "Dune",
                           "monitored": True, "hasFile": True}]
    svc.radarr.commands[8] = {"id": 8, "status": "started"}
    svc.radarr.queue = {"records": [
        {"id": 700, "movieId": 70, "downloadId": "same", "size": 100},
        {"id": 701, "movieId": 70, "downloadId": "same", "size": 100}]}
    removed_movie = svc.delete_movie(438631, [8])
    assert removed_movie["downloads_canceled"] == 1
    assert removed_movie["files_deleted"] == 1
    assert not svc.radarr.library
    assert ("command/8", None, None) in svc.radarr.deletes
    assert any(endpoint == "queue/700" and params["removeFromClient"]
               and params["skipRedownload"] and not params["blocklist"]
               for endpoint, params, _ in svc.radarr.deletes)

    svc = service()
    svc.sonarr.library = [{
        "id": 71, "tvdbId": 393189, "title": "Andor", "monitored": True,
        "seasons": [{"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": True}]}]
    svc.sonarr.episodes = [
        {"id": 710, "seriesId": 71, "seasonNumber": 1, "monitored": True,
         "hasFile": True, "episodeFileId": 810},
        {"id": 711, "seriesId": 71, "seasonNumber": 1, "monitored": True,
         "hasFile": False, "episodeFileId": 0},
        {"id": 712, "seriesId": 71, "seasonNumber": 2, "monitored": True,
         "hasFile": True, "episodeFileId": 812}]
    svc.sonarr.queue = {"records": [
        {"id": 720, "seriesId": 71, "episodeId": 710,
         "downloadId": "season-one"},
        {"id": 721, "seriesId": 71, "episodeId": 711,
         "downloadId": "season-one"},
        {"id": 722, "seriesId": 71, "episodeId": 712,
         "downloadId": "season-two"}]}
    removed_season = svc.delete_series(393189, seasons=[1])
    assert removed_season["downloads_canceled"] == 1
    assert removed_season["files_deleted"] == 1
    assert svc.sonarr.library[0]["seasons"][0]["monitored"] is False
    assert svc.sonarr.library[0]["seasons"][1]["monitored"] is True
    assert svc.sonarr.episodes[0]["hasFile"] is False
    assert svc.sonarr.episodes[2]["hasFile"] is True
    assert len(svc.sonarr.queue["records"]) == 1
    try:
        svc.delete_series(393189)
        raise AssertionError("unscoped series deletion was accepted")
    except media.MediaError:
        pass
    removed_all = svc.delete_series(393189, all_seasons=True)
    assert removed_all["all_seasons"] and not svc.sonarr.library
    print("  delete: authority commands, queue payloads, and scoped imported files")

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

    doctor_cfg = json.loads(json.dumps(_bootstrap.CONFIG))
    doctor_cfg["media"]["enabled"] = True
    doctor_cfg["media"]["protonPortSync"] = True
    doctor_secrets = {
        "radarrApiKey": "radarr-key-long-enough",
        "sonarrApiKey": "sonarr-key-long-enough",
        "prowlarrApiKey": "prowlarr-key-long-enough",
        "qbittorrentPassword": "qbit-password-long-enough",
    }

    def doctor_arr_transport(method, url, headers, body, timeout):
        split = urllib.parse.urlsplit(url)
        path = split.path
        if path.endswith("/system/status"):
            return {"version": "1.0.0", "appName": "test"}
        if path.endswith("/health"):
            return []
        if "/api/v1/indexer" in path:
            return [{
                "name": name, "enable": True,
                "fields": [{"name": "torrentBaseSettings.seedRatio",
                            "value": 0.25},
                           {"name": "torrentBaseSettings.seedTime",
                            "value": 60}],
            } for name in ("1337x", "EZTV")]
        if path.endswith("/api/v1/applications"):
            return [{"name": name, "implementation": name,
                     "syncLevel": "fullSync"}
                    for name in ("Radarr", "Sonarr")]
        if path.endswith("/rootfolder"):
            root_path = "/data/Movies" if split.port == 7878 else "/data/TV"
            return [{"path": root_path}]
        if path.endswith("/qualityprofile"):
            key = "moviePresets" if split.port == 7878 else "seriesPresets"
            return [{"name": name} for name in set(
                doctor_cfg["media"][key].values())]
        if path.endswith("/indexer"):
            return [{"name": "synced", "enable": True}]
        if path.endswith("/config/downloadclient"):
            return {"enableCompletedDownloadHandling": True}
        if path.endswith("/downloadclient"):
            category = "radarr" if split.port == 7878 else "sonarr"
            category_field = "movieCategory" if split.port == 7878 else "tvCategory"
            return [{"implementation": "QBittorrent", "enable": True,
                     "removeCompletedDownloads": True,
                     "fields": [{"name": category_field, "value": category}]}]
        raise AssertionError((method, url))

    doctor_qbit_preferences = {
        "current_network_interface": "ProtonVPN",
        "current_interface_name": "ProtonVPN",
        "current_interface_address": "",
        "upnp": False,
        "listen_port": 33125,
        "max_ratio_act": 0,
        "bypass_local_auth": False,
        "bypass_auth_subnet_whitelist_enabled": False,
    }

    def doctor_qbit_transport(method, url, headers, body, timeout):
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/auth/login"):
            return {"Set-Cookie": "SID=doctor; path=/"}, b"Ok."
        if path.endswith("/app/version"):
            return {}, b"5.1.4"
        if path.endswith("/app/preferences"):
            return {}, json.dumps(doctor_qbit_preferences).encode()
        if path.endswith("/torrents/categories"):
            return {}, json.dumps({"radarr": {}, "sonarr": {}}).encode()
        raise AssertionError((method, url))

    compose_rows = [{"Service": name, "State": "running", "Health": ""}
                    for name in ("flaresolverr", "prowlarr", "radarr", "sonarr")]
    doctor_proton_log = proton_dir / "doctor-client-logs.txt"
    doctor_proton_log.write_text(
        proton_event("2026-08-30T05:00:00.000Z",
                     "SleepingUntilRefresh", 33125), encoding="utf-8")
    doctor_now = datetime.datetime(
        2026, 8, 30, 5, 0, 5, tzinfo=datetime.timezone.utc)
    doctor = media.media_doctor(
        doctor_cfg, doctor_secrets, cglib.CapturingLog("voice"),
        arr_transport=doctor_arr_transport,
        qbit_transport=doctor_qbit_transport,
        compose_runner=lambda media_dir: compose_rows,
        proton_log_path=doctor_proton_log, now=doctor_now)
    assert doctor["ok"]
    assert [row["level"] for row in doctor["checks"]].count("WARN") == 0
    assert any(row["name"] == "qBittorrent share-limit action"
               and row["level"] == "PASS" for row in doctor["checks"])
    assert any(row["name"] == "Proton port synchronization"
               and row["level"] == "PASS" for row in doctor["checks"])
    broken_preferences = dict(doctor_qbit_preferences,
                              current_network_interface="Ethernet", upnp=True,
                              share_limits_mode="MatchAll", listen_port=1234)
    doctor_qbit_preferences.clear()
    doctor_qbit_preferences.update(broken_preferences)
    broken = media.media_doctor(
        doctor_cfg, doctor_secrets, cglib.CapturingLog("voice"),
        arr_transport=doctor_arr_transport,
        qbit_transport=doctor_qbit_transport,
        compose_runner=lambda media_dir: compose_rows,
        proton_log_path=doctor_proton_log, now=doctor_now)
    assert not broken["ok"]
    assert any(row["name"] == "qBittorrent UPnP/NAT-PMP"
               and row["level"] == "FAIL" for row in broken["checks"])
    assert any(row["name"] == "qBittorrent share-limit mode"
               and row["level"] == "FAIL" for row in broken["checks"])
    assert any(row["name"] == "Proton port synchronization"
               and row["level"] == "FAIL" for row in broken["checks"])
    print("  doctor: live boundaries and policy checks")

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
