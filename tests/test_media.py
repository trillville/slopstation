"""Radarr/Sonarr API boundary, policy, and completion evidence."""

import dataclasses
import datetime
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

import helpers
from slopstation import logbook
from slopstation.agent.tools import (
    disk_health,
    media,
    media_checks,
    media_clients,
    media_health,
    media_proton,
    operations,
)

UTC = datetime.UTC


def _records():
    return {"records": []}


@dataclasses.dataclass
class FakeArr:
    """One Arr app: answers GETs from the rows it holds and records every
    write. The rows are turned by name, so a typo is a failure rather than a
    new attribute."""

    name: str
    profiles: list = dataclasses.field(default_factory=list)
    lookup: list = dataclasses.field(default_factory=list)
    lookup_by_id: Any = None
    library: list = dataclasses.field(default_factory=list)
    episodes: list = dataclasses.field(default_factory=list)
    movie_files: list = dataclasses.field(default_factory=list)
    queue: dict = dataclasses.field(default_factory=_records)
    history: dict = dataclasses.field(default_factory=_records)
    root_folders: list = dataclasses.field(default_factory=list)
    health: list = dataclasses.field(default_factory=list)
    indexers: list = dataclasses.field(
        default_factory=lambda: [
            {"id": 1, "enable": True, "enableAutomaticSearch": True}
        ]
    )
    posts: list = dataclasses.field(default_factory=list)
    puts: list = dataclasses.field(default_factory=list)
    deletes: list = dataclasses.field(default_factory=list)
    commands: dict = dataclasses.field(default_factory=dict)
    created: Any = None

    def set(self, **rows):
        for name, value in rows.items():
            getattr(self, name)
            setattr(self, name, value)
        return self

    def get(self, endpoint, params=None):
        if endpoint == "qualityprofile":
            return list(self.profiles)
        if endpoint in ("movie/lookup", "series/lookup"):
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
        if endpoint == "history":
            return self.history
        if endpoint.startswith("command/"):
            command_id = int(endpoint.split("/")[1])
            return dict(
                self.commands.get(
                    command_id,
                    {"id": command_id, "status": "completed", "result": "successful"},
                )
            )
        if endpoint == "system/status":
            return {"version": "1.2.3", "appName": self.name}
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
                "id": command_id,
                "status": "started",
                "result": "unknown",
            }
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

    def delete(self, endpoint, params=None):
        self.deletes.append((endpoint, params))
        if endpoint.startswith("command/"):
            self.commands.pop(int(endpoint.split("/")[1]), None)
        elif endpoint.startswith("queue/"):
            queue_id = int(endpoint.split("/")[1])
            selected = next(
                row for row in self.queue["records"] if int(row["id"]) == queue_id
            )
            download_id = selected.get("downloadId")
            self.queue["records"] = [
                row
                for row in self.queue["records"]
                if (
                    row.get("downloadId") != download_id
                    if download_id
                    else int(row.get("id", 0)) != queue_id
                )
            ]
        elif endpoint.startswith(("movie/", "series/")):
            wanted = int(endpoint.split("/")[1])
            self.library[:] = [row for row in self.library if row["id"] != wanted]
        elif endpoint.startswith("episodefile/"):
            wanted = int(endpoint.split("/")[1])
            for episode in self.episodes:
                if int(episode.get("episodeFileId", 0) or 0) == wanted:
                    episode.update(hasFile=False, episodeFileId=0)
        return None


SERVICE_CFG = {
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


@pytest.fixture
def service():
    """A MediaService over two fresh Arrs whose profiles and roots match the
    config, so a request has everything it needs but the title."""
    radarr = FakeArr(
        "Radarr",
        profiles=[{"id": 10, "name": "Movie UHD"}, {"id": 11, "name": "Movie HD"}],
        root_folders=[{"id": 1, "path": "/data/Movies/"}],
    )
    sonarr = FakeArr(
        "Sonarr",
        profiles=[{"id": 20, "name": "Series HD"}, {"id": 21, "name": "Series UHD"}],
        root_folders=[{"id": 1, "path": "/data/TV"}],
    )
    return media.MediaService(
        SERVICE_CFG, logbook.CapturingLog("voice"), radarr, sonarr
    )


# --- authenticated HTTP shape -------------------------------------------------


def test_arr_client_sends_the_key_and_encodes_the_body():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        return {"ok": True}

    client = media_clients.ArrClient(
        "Radarr", "http://127.0.0.1:7878/", "secret-key", transport=transport
    )
    assert client.get("movie/lookup", {"term": "Dune 2021"}) == {"ok": True}
    client.post("command", {"name": "MoviesSearch", "movieIds": [7]})
    assert calls[0][0] == "GET" and calls[0][2]["X-Api-Key"] == "secret-key"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0][1]).query) == {
        "term": ["Dune 2021"]
    }
    assert json.loads(calls[1][3]) == {"name": "MoviesSearch", "movieIds": [7]}


@dataclasses.dataclass
class FakeQbitWeb:
    """qBittorrent's WebUI: one login cookie per session, preferences read
    back and updated, and every call recorded."""

    calls: list = dataclasses.field(default_factory=list)
    preferences: dict = dataclasses.field(default_factory=lambda: {"listen_port": 6881})

    def transport(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/auth/login"):
            return {"Set-Cookie": "QBT_SID_8080=session-1; HttpOnly; path=/"}, b""
        if headers.get("Cookie") == "QBT_SID_8080=expired":
            raise media_clients.QbittorrentAuthError("expired session")
        assert headers["Cookie"] == "QBT_SID_8080=session-1"
        if path.endswith("/app/preferences"):
            return {}, json.dumps(self.preferences).encode()
        if path.endswith("/app/setPreferences"):
            changes = json.loads(urllib.parse.parse_qs(body.decode())["json"][0])
            self.preferences.update(changes)
            return {}, b""
        raise AssertionError((method, path))

    def count(self, suffix):
        return len([row for row in self.calls if row[1].endswith(suffix)])


@pytest.fixture
def qbit_web():
    return FakeQbitWeb()


@pytest.fixture
def qbit(qbit_web):
    return media_clients.QbittorrentClient(
        "http://127.0.0.1:8080",
        "admin",
        "a-long-qbit-password",
        transport=qbit_web.transport,
    )


def test_qbittorrent_client_logs_in_once_and_sets_the_port(qbit, qbit_web, monkeypatch):
    changed_port = qbit.set_listen_port(33125)
    assert changed_port == {
        "ok": True,
        "previous_port": 6881,
        "listen_port": 33125,
        "changed": True,
    }
    assert qbit_web.calls[0][2]["Origin"] == "http://127.0.0.1:8080"
    assert urllib.parse.parse_qs(qbit_web.calls[0][3].decode()) == {
        "username": ["admin"],
        "password": ["a-long-qbit-password"],
    }
    assert qbit_web.count("/app/setPreferences") == 1
    qbit.set_listen_port(33125)
    assert qbit_web.count("/app/setPreferences") == 1
    with pytest.raises(media_clients.MediaError):
        qbit.set_listen_port(0)
    monkeypatch.setattr(qbit, "sid", "expired")
    assert qbit.preferences()["listen_port"] == 33125
    assert qbit_web.count("/auth/login") == 2


# --- Proton port forwarding ---------------------------------------------------


def proton_event(timestamp, status, port=None):
    pair = "" if port is None else f", Port pair {port}->{port}, expiring in 00:01:00"
    return (
        f"{timestamp} | INFO  | PROCESS.COMM | Received PortForwarding "
        f"Status '{status}' triggered at 'fixture'{pair} |\n"
        '{"Caller":"ClientControllerListener"}\n'
    )


PROTON_NOW = datetime.datetime(2026, 8, 30, 4, 10, 40, tzinfo=UTC)


@pytest.fixture
def proton_log(tmp_path):
    """A Proton client log that has just mapped port 39733."""
    path = tmp_path / "client-logs.txt"
    path.write_text(
        proton_event("2026-08-27T18:13:17.939Z", "Stopped")
        + proton_event("2026-08-30T04:10:26.030Z", "Starting")
        + proton_event("2026-08-30T04:10:26.031Z", "HelloCommunication")
        + proton_event("2026-08-30T04:10:26.047Z", "PortMappingCommunication")
        + proton_event("2026-08-30T04:10:36.034Z", "SleepingUntilRefresh", 39733),
        encoding="utf-8",
    )
    return path


def test_proton_log_parsing(proton_log, tmp_path):
    source = media_proton.read_proton_port_state(proton_log, now=PROTON_NOW)
    assert source["state"] == "active" and source["port"] == 39733
    assert (
        media_proton.read_proton_port_state(tmp_path / "missing.txt", now=PROTON_NOW)[
            "state"
        ]
        == "missing"
    )
    proton_log.write_text("not a Proton status line", encoding="utf-8")
    assert (
        media_proton.read_proton_port_state(proton_log, now=PROTON_NOW)["state"]
        == "unknown"
    )
    proton_backup = tmp_path / "client-logs.1.txt"
    proton_backup.write_text(
        proton_event("2026-08-30T04:12:03.000Z", "SleepingUntilRefresh", 40123),
        encoding="utf-8",
    )
    rotated = media_proton.read_proton_port_state(
        proton_log, now=datetime.datetime(2026, 8, 30, 4, 12, 4, tzinfo=UTC)
    )
    assert rotated["state"] == "active" and rotated["port"] == 40123


def test_proton_monitor_syncs_a_fresh_mapping(proton_log, qbit, qbit_web, monkeypatch):
    qbit_web.preferences["listen_port"] = 33125
    proton_monitor = media_proton.ProtonPortMonitor(
        qbit, logbook.CapturingLog("voice"), path=proton_log, now=PROTON_NOW
    )
    synced = proton_monitor.reconcile_once()
    assert synced["changed"] and synced["previous_port"] == 33125
    assert synced["listen_port"] == 39733
    mutations = qbit_web.count("/app/setPreferences")
    assert not proton_monitor.reconcile_once()["changed"]
    assert qbit_web.count("/app/setPreferences") == mutations

    proton_log.write_text(
        proton_log.read_text(encoding="utf-8")
        + proton_event(
            "2026-08-30T04:10:41.000Z", "DestroyPortMappingCommunication", 39733
        )
        + proton_event("2026-08-30T04:10:41.100Z", "Stopped"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 10, 42, tzinfo=UTC)
    )
    assert proton_monitor.reconcile_once()["state"] == "inactive"
    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 12, 0, tzinfo=UTC)
    )
    assert proton_monitor.inspect()["state"] == "stale"
    with pytest.raises(media_clients.MediaError):
        proton_monitor.reconcile_once()
    proton_log.write_text(
        proton_event("2026-08-30T04:12:01.000Z", "Starting"), encoding="utf-8"
    )
    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 12, 2, tzinfo=UTC)
    )
    assert proton_monitor.reconcile_once()["state"] == "transitional"


# --- media health watch -------------------------------------------------------


def test_health_watch_reports_transitions_once():
    watch_radarr = FakeArr(
        "Radarr",
        health=[
            {
                "source": "IndexerStatusCheck",
                "type": "warning",
                "message": "Indexers unavailable due to failures",
            }
        ],
        history={
            "records": [
                {"id": 4, "eventType": "grabbed", "sourceTitle": "Dune.2021"},
                {
                    "id": 5,
                    "eventType": "downloadFailed",
                    "sourceTitle": "Dune.2021",
                    "data": {"message": "Torrent removed by qBittorrent"},
                },
            ]
        },
    )
    watch_sonarr = FakeArr(
        "Sonarr",
        queue={
            "records": [
                {
                    "id": 1,
                    "downloadId": "ABC",
                    "title": "Show.S01",
                    "trackedDownloadStatus": "warning",
                    "statusMessages": [{"messages": ["Not a preferred word upgrade"]}],
                },
                {
                    "id": 2,
                    "downloadId": "ABC",
                    "title": "Show.S01",
                    "trackedDownloadStatus": "warning",
                    "statusMessages": [{"messages": ["Not a preferred word upgrade"]}],
                },
            ]
        },
    )
    watch_log = logbook.CapturingLog("voice")
    watch = media_health.MediaHealthMonitor((watch_radarr, watch_sonarr), watch_log)

    watch.reconcile_once()
    issue = watch_log.find("media_health_issue")
    assert len(issue) == 1 and issue[0]["source"] == "IndexerStatusCheck"
    assert issue[0]["level"] == "warn" and issue[0]["app"] == "Radarr"
    # History already on disk at startup is backlog, not news.
    assert not watch_log.find("media_import_failed")
    # A season pack is one queue row per episode and one thing to act on.
    stalled = watch_log.find("media_queue_stalled")
    assert len(stalled) == 1 and stalled[0]["download"] == "ABC"

    watch_log.records.clear()
    watch.reconcile_once()
    assert watch_log.events() == []

    watch_log.records.clear()
    watch_radarr.set(health=[])
    watch_radarr.history["records"].extend(
        [
            {
                "id": 6,
                "eventType": "importFailed",
                "sourceTitle": "Heat.1995",
                "downloadId": "PACK",
                "episodeId": 1,
                "data": {"message": "No files found are eligible for import"},
            },
            {
                "id": 7,
                "eventType": "importFailed",
                "sourceTitle": "Heat.1995",
                "downloadId": "PACK",
                "episodeId": 2,
                "data": {"message": "No files found are eligible for import"},
            },
        ]
    )
    watch_sonarr.queue["records"][0]["trackedDownloadStatus"] = "error"
    watch.reconcile_once()
    failed = watch_log.find("media_import_failed")
    # One bad grab is one line even though it failed once per episode.
    assert [r["title"] for r in failed] == ["Heat.1995"]
    assert failed[0]["records"] == 2
    assert failed[0]["level"] == "error" and failed[0]["kind"] == "importFailed"
    assert watch_log.find("media_health_cleared")[0]["source"] == "IndexerStatusCheck"
    assert watch_log.find("media_queue_stalled")[0]["status"] == "error"


def test_health_watch_reports_a_dead_app_once():
    class DeadArr:
        name = "Sonarr"

        def get(self, endpoint, params=None):
            raise media_clients.MediaError("connection refused")

    watch_log = logbook.CapturingLog("voice")
    dead = media_health.MediaHealthMonitor((DeadArr(),), watch_log)
    dead.reconcile_once()
    dead.reconcile_once()
    # An app that stays down is one line, not one line per poll.
    assert len(watch_log.find("media_watch_failed")) == 1


def test_health_watch_is_off_when_config_says_so():
    assert (
        media.media_health_monitor_from_config(
            {"media": {"enabled": True, "healthSync": False}},
            {},
            logbook.CapturingLog("voice"),
        )
        is None
    )


# --- grabs no operation asked for ---------------------------------------------


@dataclasses.dataclass
class FakeLedger:
    rows: list

    def active(self, kind=None):
        return list(self.rows)


def test_unattributed_grabs_are_reported_per_download():
    grab_sonarr = FakeArr("Sonarr")
    watch_log = logbook.CapturingLog("voice")
    grabs = media_health.MediaHealthMonitor(
        (grab_sonarr,),
        watch_log,
        operations=FakeLedger([{"authority": "sonarr", "external_ref": "3"}]),
    )
    grabs.reconcile_once()
    grab_sonarr.history["records"].extend(
        [
            {
                "id": 20,
                "eventType": "grabbed",
                "seriesId": 3,
                "downloadId": "MINE",
                "sourceTitle": "Asked.For.S05",
                "data": {"indexer": "1337x"},
            },
            {
                "id": 21,
                "eventType": "grabbed",
                "seriesId": 9,
                "downloadId": "LOOSE",
                "sourceTitle": "Nobody.Asked.S01",
                "data": {"indexer": "1337x"},
            },
            {
                "id": 22,
                "eventType": "grabbed",
                "seriesId": 9,
                "downloadId": "LOOSE",
                "sourceTitle": "Nobody.Asked.S01",
                "data": {"indexer": "1337x"},
            },
        ]
    )
    grabs.reconcile_once()
    loose = watch_log.find("media_grab_unattributed")
    # The owned grab stays silent; the season pack is one line, not two.
    assert [r["title"] for r in loose] == ["Nobody.Asked.S01"]
    assert loose[0]["records"] == 2 and loose[0]["indexer"] == "1337x"
    assert loose[0]["level"] == "info" and loose[0]["app"] == "Sonarr"


def test_no_ledger_means_no_attribution():
    grab_sonarr = FakeArr("Sonarr")
    watch_log = logbook.CapturingLog("voice")
    blind = media_health.MediaHealthMonitor((grab_sonarr,), watch_log)
    blind.reconcile_once()
    grab_sonarr.history["records"].append(
        {
            "id": 23,
            "eventType": "grabbed",
            "seriesId": 9,
            "downloadId": "Z",
            "sourceTitle": "Still.Nobody.S01",
            "data": {"indexer": "1337x"},
        }
    )
    blind.reconcile_once()
    # No ledger means no attribution, so the row stays quiet rather than
    # calling every grab unattributed.
    assert not watch_log.find("media_grab_unattributed")


# --- disk watch ---------------------------------------------------------------

GB = 1024**3


@dataclasses.dataclass
class Usage:
    total: int
    free: int

    @property
    def used(self):
        return self.total - self.free


@dataclasses.dataclass
class FakeShutil:
    table: dict

    def disk_usage(self, mount):
        value = self.table[mount]
        if isinstance(value, Exception):
            raise value
        return value


def test_disk_watch_reports_crossings(monkeypatch):
    table = {"M:": Usage(1000 * GB, 100 * GB), "C:": Usage(1000 * GB, 900 * GB)}
    monkeypatch.setattr(disk_health, "shutil", FakeShutil(table))
    disk_log = logbook.CapturingLog("voice")
    disk = disk_health.DiskHealthMonitor(
        ("M:", "C:"), disk_log, free_warn_bytes=250 * GB
    )
    disk.reconcile_once()
    low = disk_log.find("disk_space_low")
    # The roomy volume is silent; only the one below the threshold reports.
    assert len(low) == 1 and low[0]["mount"] == "M:"
    assert low[0]["free_gb"] == 100.0 and low[0]["pct_free"] == 10.0
    assert low[0]["level"] == "warn"
    disk.reconcile_once()
    # A full disk stays full: the crossing is the news, not the state.
    assert len(disk_log.find("disk_space_low")) == 1

    table["M:"] = Usage(1000 * GB, 600 * GB)
    disk.reconcile_once()
    assert len(disk_log.find("disk_space_cleared")) == 1
    table["M:"] = Usage(1000 * GB, 100 * GB)
    disk.reconcile_once()
    # Cleared re-arms, or a drive that oscillates would report once ever.
    assert len(disk_log.find("disk_space_low")) == 2

    disk_log.records.clear()
    table["M:"] = OSError("the device is not ready")
    disk.reconcile_once()
    disk.reconcile_once()
    # An unplugged enclosure is one line, not one line per poll.
    failed = disk_log.find("disk_watch_failed")
    assert len(failed) == 1 and failed[0]["mount"] == "M:"


def test_disk_watch_needs_config_and_a_host_root():
    disk_log = logbook.CapturingLog("voice")
    assert (
        media.disk_health_monitor_from_config(
            {"media": {"enabled": True, "diskWatch": False}}, disk_log
        )
        is None
    )
    # No .env means no host root to resolve: a checkout that is not the K15
    # runs the supervisor without inventing a volume to watch.
    assert (
        media.disk_health_monitor_from_config(
            {"media": {"enabled": True}}, disk_log, env_path=Path("no-such.env")
        )
        is None
    )


# --- catalog lookups ----------------------------------------------------------


def test_find_trims_and_caps_results(service):
    service.radarr.set(
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
    found = service.find("movie", "Movie")
    assert len(found) == 5 and found[0] == {
        "tmdb_id": 1,
        "title": "Movie 1",
        "year": 2001,
        "status": "released",
    }
    assert "overview" not in found[0]


# --- library ------------------------------------------------------------------


def test_library_reports_holdings_per_season(service):
    lib = service
    assert lib.library("movie", 438631) == {
        "kind": "movie",
        "catalog_id": 438631,
        "in_library": False,
    }
    lib.radarr.set(
        library=[{"id": 32, "tmdbId": 438631, "title": "Dune", "hasFile": True}]
    )
    held = lib.library("movie", 438631)
    assert held["in_library"] and held["available"] and held["title"] == "Dune"
    assert lib.library("series", 81189)["in_library"] is False
    lib.sonarr.set(
        library=[{"id": 41, "tvdbId": 81189, "title": "Breaking Bad"}],
        episodes=[
            {
                "id": 1,
                "seasonNumber": 1,
                "hasFile": True,
                "monitored": False,
                "airDateUtc": "2008-01-20T00:00:00Z",
            },
            {
                "id": 2,
                "seasonNumber": 1,
                "hasFile": False,
                "monitored": False,
                "airDateUtc": "2008-01-27T00:00:00Z",
            },
            {
                "id": 3,
                "seasonNumber": 2,
                "hasFile": True,
                "monitored": True,
                "airDateUtc": "2009-03-08T00:00:00Z",
            },
            {
                "id": 4,
                "seasonNumber": 2,
                "hasFile": False,
                "monitored": True,
                "airDateUtc": "2999-01-01T00:00:00Z",
            },
            {
                "id": 5,
                "seasonNumber": 0,
                "hasFile": True,
                "monitored": True,
                "airDateUtc": "2009-01-01T00:00:00Z",
            },
        ],
    )
    owned = lib.library("series", 81189)
    assert owned["title"] == "Breaking Bad"
    assert owned["seasons"] == [
        {"season": 1, "have": 1, "aired": 2},
        {"season": 2, "have": 1, "aired": 1},
    ]
    with pytest.raises(media_clients.MediaError):
        lib.library("album", 1)


# --- abandon ------------------------------------------------------------------


def test_abandon_missing_unmonitors_the_gap(service):
    aband = service
    aband.sonarr.set(
        library=[{"id": 41, "tvdbId": 81189, "title": "Breaking Bad"}],
        episodes=[
            {
                "id": 1,
                "seasonNumber": 1,
                "hasFile": False,
                "monitored": True,
                "airDateUtc": "2008-01-20T00:00:00Z",
            },
            {
                "id": 2,
                "seasonNumber": 1,
                "hasFile": False,
                "monitored": True,
                "airDateUtc": "2008-01-27T00:00:00Z",
            },
            {
                "id": 3,
                "seasonNumber": 2,
                "hasFile": True,
                "monitored": True,
                "airDateUtc": "2009-03-08T00:00:00Z",
            },
        ],
    )
    result = aband.abandon_missing(
        {
            "kind": "series_acquisition",
            "external_ref": "41",
            "metadata": {"seasons": None},
        }
    )
    assert result == {"have": 1, "missing": [{"season": 1, "episodes": 2}]}
    assert aband.sonarr.puts[-1] == (
        "episode/monitor",
        {"episodeIds": [1, 2], "monitored": False},
    )
    aband.radarr.set(
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
    result = aband.abandon_missing({"kind": "movie_acquisition", "external_ref": "32"})
    assert result == {"have": 0, "missing": []}
    assert aband.radarr.puts[-1][1]["monitored"] is False


# --- movies -------------------------------------------------------------------


def test_request_movie_adds_and_searches(service):
    svc = service
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


def test_request_movie_changes_the_profile_and_skips_what_is_held(service):
    svc = service
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


def test_movie_upgrade_completes_on_a_new_file(service):
    svc = service
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
    assert not svc.observe(operation)["complete"]
    svc.radarr.movie_files[0]["id"] = 72
    assert svc.observe(operation)["complete"]


# --- selected series seasons --------------------------------------------------


def test_request_series_monitors_only_the_asked_seasons(service):
    svc = service
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
    monitored = {
        r["seasonNumber"]: r["monitored"] for r in svc.sonarr.puts[0][1]["seasons"]
    }
    assert monitored == {0: False, 1: False, 2: True}
    assert not [post for post in svc.sonarr.posts if post[0] == "command"]
    assert series["seasons"] == [2] and series["search_pending"]
    pending = {
        "kind": "series_acquisition",
        "external_ref": "41",
        "metadata": {"seasons": [2], "search_pending": True},
    }
    assert not svc.dispatch_pending_series_search(pending)
    svc.sonarr.set(
        episodes=[
            {
                "id": 101,
                "seasonNumber": 0,
                "monitored": False,
                "hasFile": False,
                "airDateUtc": "2019-01-01T00:00:00Z",
            },
            {
                "id": 102,
                "seasonNumber": 2,
                "monitored": False,
                "hasFile": False,
                "airDateUtc": "2020-01-01T00:00:00Z",
            },
            {
                "id": 103,
                "seasonNumber": 2,
                "monitored": True,
                "hasFile": False,
                "airDateUtc": "2020-01-08T00:00:00Z",
            },
        ]
    )
    assert svc.dispatch_pending_series_search(pending)
    assert svc.sonarr.puts[-1] == (
        "episode/monitor",
        {"episodeIds": [102], "monitored": True},
    )
    assert svc.sonarr.posts[-1] == (
        "command",
        {"name": "SeasonSearch", "seriesId": 41, "seasonNumber": 2},
    )
    observation = svc.observe_series(41, [2])
    assert observation["progress"] == {
        "episodes": 0,
        "total_episodes": 2,
        "percent": 0,
        "phase": "waiting_for_match",
    }
    with pytest.raises(media_clients.MediaError):
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


def test_search_retry_waits_for_a_search_capable_indexer(service):
    svc = service
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


def test_series_upgrade_completes_on_new_episode_files(service):
    svc = service
    svc.sonarr.set(
        library=[
            {
                "id": 42,
                "tvdbId": 81189,
                "title": "Breaking Bad",
                "qualityProfileId": 20,
                "seasons": [{"seasonNumber": 1, "monitored": True}],
            }
        ],
        episodes=[
            {
                "id": 101,
                "episodeFileId": 201,
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": True,
                "airDateUtc": "2020-01-01T00:00:00Z",
            },
            {
                "id": 102,
                "episodeFileId": 0,
                "seasonNumber": 1,
                "monitored": True,
                "hasFile": False,
                "airDateUtc": "2020-01-08T00:00:00Z",
            },
        ],
    )
    series_upgrade = svc.request_series(81189, "2160p", [1])
    assert series_upgrade["baseline_episode_files"] == {"101": 201}
    upgrade_operation = {
        "kind": "series_acquisition",
        "external_ref": "42",
        "metadata": {"seasons": [1], "baseline_episode_files": {"101": 201}},
    }
    assert not svc.observe(upgrade_operation)["complete"]
    svc.sonarr.episodes[0]["episodeFileId"] = 301
    svc.sonarr.episodes[1].update(hasFile=True, episodeFileId=302)
    assert svc.observe(upgrade_operation)["complete"]


def test_season_scoping_keeps_what_others_monitored(service):
    # A series Slopstation creates is scoped exactly; one that was already in
    # the library keeps the seasons somebody else monitored, which is what
    # lets a part-aired season keep filling in after the operation closes.
    library_row = {
        "seasons": [
            {"seasonNumber": 1, "monitored": True},
            {"seasonNumber": 2, "monitored": False},
            {"seasonNumber": 3, "monitored": True},
        ]
    }
    kept = service._set_series_seasons(library_row, [2])
    assert [s["monitored"] for s in kept["seasons"]] == [True, True, True]
    scoped = service._set_series_seasons(library_row, [2], exclusive=True)
    assert [s["monitored"] for s in scoped["seasons"]] == [False, True, False]


# --- positive completion evidence ---------------------------------------------


def test_observe_movie_reports_the_download_without_its_title(service):
    svc = service
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
    assert not movie_progress["complete"]
    assert movie_progress["progress"] == {"phase": "downloading", "percent": 75}
    assert "UNTRUSTED" not in movie_progress["detail"]
    svc.radarr.library[0]["hasFile"] = True
    assert svc.observe_movie(50)["complete"]


def test_observe_series_counts_aired_monitored_episodes(service):
    svc = service
    now = datetime.datetime(2026, 8, 29, tzinfo=UTC)
    empty = svc.observe_series(60, [1], now)
    assert not empty["metadata_ready"]
    assert empty["detail"] == "Sonarr is still populating episode metadata"
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
    assert progress["metadata_ready"]
    assert not progress["complete"]
    assert progress["progress"] == {
        "episodes": 1,
        "total_episodes": 2,
        "percent": 50,
        "phase": "waiting_for_match",
    }
    svc.sonarr.episodes[2]["hasFile"] = True
    assert svc.observe_series(60, None, now)["complete"]
    for episode in svc.sonarr.episodes:
        if episode["seasonNumber"] == 1:
            episode["monitored"] = False
    canceled = svc.observe_series(60, [1], now)
    assert canceled["canceled"] and not canceled["complete"]


# --- authoritative abandonment ------------------------------------------------


def test_delete_movie_cancels_its_downloads(service):
    svc = service
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
    removed_movie = svc.delete_movie(438631, [8])
    assert removed_movie["downloads_canceled"] == 1
    assert removed_movie["files_deleted"] == 1
    assert not svc.radarr.library
    assert ("command/8", None) in svc.radarr.deletes
    assert any(
        endpoint == "queue/700"
        and params["removeFromClient"]
        and params["skipRedownload"]
        and not params["blocklist"]
        for endpoint, params in svc.radarr.deletes
    )


def test_delete_series_is_scoped_to_seasons(service):
    svc = service
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
    assert svc.sonarr.library[0]["seasons"][0]["monitored"] is False
    assert svc.sonarr.library[0]["seasons"][1]["monitored"] is True
    assert svc.sonarr.episodes[0]["hasFile"] is False
    assert svc.sonarr.episodes[2]["hasFile"] is True
    assert len(svc.sonarr.queue["records"]) == 1
    with pytest.raises(media_clients.MediaError):
        svc.delete_series(393189)
    removed_all = svc.delete_series(393189, all_seasons=True)
    assert removed_all["all_seasons"] and not svc.sonarr.library


# --- factory gating and status ------------------------------------------------


def test_from_config_needs_the_lane_and_its_keys():
    cfg = json.loads(json.dumps(helpers.CONFIG))
    log = logbook.CapturingLog("voice")
    assert media.from_config(cfg, {}, log) is None
    cfg["media"]["enabled"] = True
    assert media.from_config(cfg, {}, log) is None
    assert log.find("lane_disabled")[-1]["what"] == "media"


def test_status_reports_app_versions(service):
    status = service.status()
    assert status["radarr"]["version"] == "1.2.3"


def test_validate_names_missing_profiles(service):
    validation = service.validate()
    assert validation["ok"] and validation["checks"]["movie"]["root_exists"]
    service.radarr.profiles.pop()
    invalid = service.validate()
    assert not invalid["ok"]
    assert invalid["checks"]["movie"]["missing_profiles"] == ["Movie HD"]


def test_track_survives_a_failing_store():
    failed_submission = {
        "ok": True,
        "kind": "movie_acquisition",
        "authority": "radarr",
        "external_ref": "31",
        "title": "Dune",
        "catalog_id": 438631,
        "preset": "default",
        "profile": "Movie UHD",
        "already_available": False,
    }

    class FailingStore:
        log = logbook.CapturingLog("voice")

        def track_external(self, *args, **kwargs):
            raise OSError("disk unavailable")

    assert operations.track(FailingStore(), failed_submission)["tracking"] == "failed"


# --- media doctor -------------------------------------------------------------

DOCTOR_SECRETS = {
    "radarrApiKey": "radarr-key-long-enough",
    "sonarrApiKey": "sonarr-key-long-enough",
    "prowlarrApiKey": "prowlarr-key-long-enough",
    "qbittorrentPassword": "qbit-password-long-enough",
}

HEALTHY_QBIT_PREFERENCES = {
    "current_network_interface": "ProtonVPN",
    "current_interface_name": "ProtonVPN",
    "current_interface_address": "",
    "upnp": False,
    "listen_port": 33125,
    "max_ratio_act": 0,
    "bypass_local_auth": False,
    "bypass_auth_subnet_whitelist_enabled": False,
}


def _doctor(tmp_path, qbit_preferences):
    """media_doctor over a stack whose only variable is what qBittorrent
    reports for its preferences."""
    doctor_cfg = json.loads(json.dumps(helpers.CONFIG))
    doctor_cfg["media"]["enabled"] = True
    doctor_cfg["media"]["protonPortSync"] = True

    def doctor_arr_transport(method, url, headers, body, timeout):
        split = urllib.parse.urlsplit(url)
        path = split.path
        if path.endswith("/system/status"):
            return {"version": "1.0.0", "appName": "test"}
        if path.endswith("/health"):
            return []
        if "/api/v1/indexer" in path:
            return [
                {
                    "name": name,
                    "enable": True,
                    "fields": [
                        {"name": "torrentBaseSettings.seedRatio", "value": 0.25},
                        {"name": "torrentBaseSettings.seedTime", "value": 60},
                    ],
                }
                for name in ("1337x", "EZTV")
            ]
        if path.endswith("/api/v1/applications"):
            return [
                {"name": name, "implementation": name, "syncLevel": "fullSync"}
                for name in ("Radarr", "Sonarr")
            ]
        if path.endswith("/rootfolder"):
            root_path = "/data/Movies" if split.port == 7878 else "/data/TV"
            return [{"path": root_path}]
        if path.endswith("/qualityprofile"):
            key = "moviePresets" if split.port == 7878 else "seriesPresets"
            return [{"name": name} for name in set(doctor_cfg["media"][key].values())]
        if path.endswith("/indexer"):
            return [{"name": "synced", "enable": True}]
        if path.endswith("/config/downloadclient"):
            return {"enableCompletedDownloadHandling": True}
        if path.endswith("/downloadclient"):
            category = "radarr" if split.port == 7878 else "sonarr"
            category_field = "movieCategory" if split.port == 7878 else "tvCategory"
            return [
                {
                    "implementation": "QBittorrent",
                    "enable": True,
                    "removeCompletedDownloads": True,
                    "fields": [{"name": category_field, "value": category}],
                }
            ]
        raise AssertionError((method, url))

    def doctor_qbit_transport(method, url, headers, body, timeout):
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/auth/login"):
            return {"Set-Cookie": "SID=doctor; path=/"}, b"Ok."
        if path.endswith("/app/version"):
            return {}, b"5.1.4"
        if path.endswith("/app/preferences"):
            return {}, json.dumps(qbit_preferences).encode()
        if path.endswith("/torrents/categories"):
            return {}, json.dumps({"radarr": {}, "sonarr": {}}).encode()
        raise AssertionError((method, url))

    compose_rows = [
        {"Service": name, "State": "running", "Health": ""}
        for name in (
            "flaresolverr",
            "prowlarr",
            "radarr",
            "sonarr",
            "homarr",
            "glances",
        )
    ]
    doctor_proton_log = tmp_path / "doctor-client-logs.txt"
    doctor_proton_log.write_text(
        proton_event("2026-08-30T05:00:00.000Z", "SleepingUntilRefresh", 33125),
        encoding="utf-8",
    )
    return media_checks.media_doctor(
        doctor_cfg,
        DOCTOR_SECRETS,
        logbook.CapturingLog("voice"),
        arr_transport=doctor_arr_transport,
        qbit_transport=doctor_qbit_transport,
        compose_runner=lambda media_dir: compose_rows,
        proton_log_path=doctor_proton_log,
        now=datetime.datetime(2026, 8, 30, 5, 0, 5, tzinfo=UTC),
    )


def test_media_doctor_passes_a_healthy_stack(tmp_path):
    doctor = _doctor(tmp_path, dict(HEALTHY_QBIT_PREFERENCES))
    assert doctor["ok"]
    assert [row["level"] for row in doctor["checks"]].count("WARN") == 0
    assert any(
        row["name"] == "qBittorrent share-limit action" and row["level"] == "PASS"
        for row in doctor["checks"]
    )
    assert any(
        row["name"] == "Proton port synchronization" and row["level"] == "PASS"
        for row in doctor["checks"]
    )


def test_media_doctor_fails_a_misconfigured_qbittorrent(tmp_path):
    broken_preferences = dict(
        HEALTHY_QBIT_PREFERENCES,
        current_network_interface="Ethernet",
        upnp=True,
        share_limits_mode="MatchAll",
        listen_port=1234,
    )
    broken = _doctor(tmp_path, broken_preferences)
    assert not broken["ok"]
    assert any(
        row["name"] == "qBittorrent UPnP/NAT-PMP" and row["level"] == "FAIL"
        for row in broken["checks"]
    )
    assert any(
        row["name"] == "qBittorrent share-limit mode" and row["level"] == "FAIL"
        for row in broken["checks"]
    )
    assert any(
        row["name"] == "Proton port synchronization" and row["level"] == "FAIL"
        for row in broken["checks"]
    )


# --- the compose stack on disk ------------------------------------------------


def test_compose_stack_layout():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "media" / "compose.yaml").read_text(encoding="utf-8")
    start_media = (root / "media" / "Start-Media.ps1").read_text(encoding="utf-8")
    for sidecar in (
        "flaresolverr:",
        "prowlarr:",
        "radarr:",
        "sonarr:",
        "homarr:",
        "glances:",
    ):
        assert sidecar in compose
    assert "qbittorrent:" not in compose
    assert "ghcr.io/flaresolverr/flaresolverr:latest" in compose
    assert "8191:8191" not in compose
    # The Arrs' two /data mounts plus Glances' read-only fill gauge.
    assert compose.count("source: ${MEDIA_ROOT}") == 3
    # Web UIs are LAN-wide (runbook firewall rules scope them); only
    # FlareSolverr and Glances stay off the host entirely.
    assert "9696:9696" in compose
    assert "7878:7878" in compose
    assert "8989:8989" in compose
    assert "127.0.0.1:" not in compose
    # Homarr is LAN-wide by design, and host 7575 belongs to VirtualHere.
    assert "ghcr.io/homarr-labs/homarr:latest" in compose
    assert "8575:7575" in compose
    assert "127.0.0.1:8575" not in compose
    assert "7575:7575" not in compose
    # Glances is internal-only, and its drive gauges depend on the 9p allow.
    assert "nicolargo/glances:latest-full" in compose
    assert "61208:61208" not in compose
    assert "/var/run/docker.sock" in compose
    glances_conf = (root / "media" / "glances.conf").read_text(encoding="utf-8")
    assert "allow=9p" in glances_conf
    assert "--remove-orphans" in start_media
    assert "logs qbittorrent" not in start_media
    assert "SECRET_ENCRYPTION_KEY" in start_media
    assert helpers.CONFIG["media"]["movieRoot"] == "/data/Movies"
    assert helpers.CONFIG["media"]["seriesRoot"] == "/data/TV"
