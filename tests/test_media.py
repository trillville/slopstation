"""Radarr/Sonarr API boundary, policy, and completion evidence."""

import collections
import dataclasses
import datetime
import json
import subprocess
import urllib.parse

import pytest

import helpers
from helpers import CapturingLog, FakeArr
from slopstation.agent.tools import (
    disk_health,
    media,
    media_checks,
    media_clients,
    media_health,
    media_proton,
    media_updates,
    operations,
)

UTC = datetime.UTC


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
    # Every call takes the LAN timeout except the indexer fan-out.
    client.get("release", {"episodeId": 9}, timeout=media_clients.SEARCH_TIMEOUT_S)
    assert [c[4] for c in calls] == [
        media_clients.HTTP_TIMEOUT_S,
        media_clients.HTTP_TIMEOUT_S,
        media_clients.SEARCH_TIMEOUT_S,
    ]


@dataclasses.dataclass
class FakeQbitWeb:
    """qBittorrent's WebUI: one login cookie per session, preferences read
    back and updated, and every call recorded."""

    calls: list = dataclasses.field(default_factory=list)
    preferences: dict = dataclasses.field(default_factory=lambda: {"listen_port": 6881})
    interfaces: list = dataclasses.field(
        default_factory=lambda: [{"name": "ProTUN", "value": "iftype53_2"}]
    )
    torrents: list = dataclasses.field(
        default_factory=lambda: [{"hash": "a" * 40, "name": "x"}]
    )
    dht_nodes: int = 200
    alive: bool = True

    def transport(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        path = urllib.parse.urlsplit(url).path
        if not self.alive:
            raise media_clients.MediaError("qBittorrent is unreachable")
        if path.endswith("/auth/login"):
            return {"Set-Cookie": "QBT_SID_8080=session-1; HttpOnly; path=/"}, b""
        if headers.get("Cookie") == "QBT_SID_8080=expired":
            raise media_clients.QbittorrentAuthError("expired session")
        assert headers["Cookie"] == "QBT_SID_8080=session-1"
        if path.endswith("/app/version"):
            return {}, b"v5.2.3"
        if path.endswith("/app/preferences"):
            return {}, json.dumps(self.preferences).encode()
        if path.endswith("/app/setPreferences"):
            changes = json.loads(urllib.parse.parse_qs(body.decode())["json"][0])
            self.preferences.update(changes)
            return {}, b""
        if path.endswith("/app/networkInterfaceList"):
            return {}, json.dumps(self.interfaces).encode()
        if path.endswith("/app/shutdown"):
            self.alive = False
            return {}, b""
        if path.endswith("/torrents/info"):
            return {}, json.dumps(self.torrents).encode()
        if path.endswith("/torrents/stop"):
            return {}, b""
        if path.endswith("/transfer/info"):
            return {}, json.dumps({"dht_nodes": self.dht_nodes}).encode()
        if path.endswith("/transfer/speedLimitsMode"):
            return {}, b"1"
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


def test_qbittorrent_client_torrent_calls_carry_params_and_form_fields(qbit, qbit_web):
    rows = qbit.torrents(filter="downloading", sort="added_on", reverse=True)
    assert rows[0]["hash"] == "a" * 40
    method, url, headers, body, _ = qbit_web.calls[-1]
    assert method == "GET" and url.endswith(
        "/torrents/info?filter=downloading&sort=added_on&reverse=true"
    )
    qbit.torrent_action("stop", ["a" * 40, "b" * 40])
    method, url, headers, body, _ = qbit_web.calls[-1]
    assert method == "POST" and url.endswith("/torrents/stop")
    assert urllib.parse.parse_qs(body.decode()) == {
        "hashes": ["a" * 40 + "|" + "b" * 40]
    }
    assert qbit.speed_limits_mode() is True
    # The passthrough shape: JSON when it parses, text otherwise, None when empty.
    assert qbit.call("GET", "torrents/info")[0]["name"] == "x"
    assert qbit.call("GET", "transfer/speedLimitsMode") == 1
    assert qbit.call("POST", "torrents/stop", payload={"hashes": "all"}) is None


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


def test_qbittorrent_rebind_resolves_the_adapter_by_name(qbit, qbit_web):
    """The adapter's current id is what gets written; an id that already
    matches is cleared first so libtorrent reopens the sockets anyway."""
    qbit_web.preferences["current_network_interface"] = "iftype53_1"
    drifted = qbit.rebind_interface("protun")
    assert drifted == {"interface": "protun", "previous": "iftype53_1", "drifted": True}
    assert qbit_web.preferences["current_network_interface"] == "iftype53_2"
    assert qbit_web.count("/app/setPreferences") == 1
    same = qbit.rebind_interface("ProTUN")
    assert not same["drifted"]
    writes = [
        json.loads(urllib.parse.parse_qs(body.decode())["json"][0])
        for _, url, _, body, _ in qbit_web.calls
        if url.endswith("/app/setPreferences")
    ]
    assert writes[1:] == [
        {"current_network_interface": ""},
        {"current_network_interface": "iftype53_2"},
    ]
    with pytest.raises(media_clients.MediaError, match="no network interface"):
        qbit.rebind_interface("Ethernet")


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
        qbit, CapturingLog("voice"), path=proton_log, now=PROTON_NOW
    )
    synced = proton_monitor.reconcile_once()
    assert synced["changed"] and synced["previous_port"] == 33125
    assert synced["listen_port"] == 39733
    mutations = qbit_web.count("/app/setPreferences")
    assert not proton_monitor.reconcile_once()["changed"]
    assert qbit_web.count("/app/setPreferences") == mutations

    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 12, 0, tzinfo=UTC)
    )
    with pytest.raises(media_clients.MediaError, match="stale"):
        proton_monitor.reconcile_once()
    proton_log.write_text(
        proton_event("2026-08-30T04:12:01.000Z", "Starting"), encoding="utf-8"
    )
    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 12, 2, tzinfo=UTC)
    )
    assert proton_monitor.reconcile_once()["state"] == "transitional"


def test_proton_reconnect_rebinds_even_when_the_port_is_unchanged(
    proton_log, qbit, qbit_web, monkeypatch
):
    log = CapturingLog("voice")
    proton_monitor = media_proton.ProtonPortMonitor(
        qbit, log, path=proton_log, now=PROTON_NOW, interface="ProTUN"
    )
    proton_monitor.reconcile_once()
    assert not log.find("qbit_rebound"), "a first poll is not a reconnect"
    proton_log.write_text(
        proton_log.read_text(encoding="utf-8")
        + proton_event("2026-08-30T04:10:41.000Z", "Stopped"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 10, 42, tzinfo=UTC)
    )
    assert proton_monitor.reconcile_once()["state"] == "inactive"
    proton_log.write_text(
        proton_log.read_text(encoding="utf-8")
        + proton_event("2026-08-30T04:10:50.000Z", "SleepingUntilRefresh", 39733),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proton_monitor, "now", datetime.datetime(2026, 8, 30, 4, 10, 51, tzinfo=UTC)
    )
    back = proton_monitor.reconcile_once()
    assert back["state"] == "active" and not back["changed"]
    assert log.find("qbit_rebound")[-1]["reason"] == "proton_reconnect"


def test_dead_peers_rebind_then_restart_then_report(
    proton_log, qbit, qbit_web, monkeypatch
):
    """The self-healing loop: zero DHT nodes with downloads waiting gets a
    rebind after PEERS_DEAD_S, a restart after another, one error after a
    third, then silence until the nodes come back."""
    monkeypatch.setattr(media_proton, "PROTON_LOG_MAX_AGE_S", 10**6)
    monkeypatch.setattr(media_proton, "reserved_port_range", lambda port: None)
    log = CapturingLog("voice")
    launched = []

    class Process:
        pid = 4242

        def poll(self):
            return None

    def launch(exe):
        launched.append(exe)
        qbit_web.alive = True
        return Process()

    qbit_web.dht_nodes = 0
    proton_monitor = media_proton.ProtonPortMonitor(
        qbit,
        log,
        path=proton_log,
        now=PROTON_NOW,
        interface="ProTUN",
        exe="C:/qb/qbittorrent.exe",
        launch=launch,
        sleep=lambda s: None,
    )

    def at(seconds):
        monkeypatch.setattr(
            proton_monitor, "now", PROTON_NOW + datetime.timedelta(seconds=seconds)
        )
        return proton_monitor.reconcile_once()

    assert at(0)["dht_nodes"] == 0
    at(299)
    assert not log.find("qbit_peers_lost")
    at(300)
    assert log.find("qbit_peers_lost")[-1]["dead_s"] == 300
    assert log.find("qbit_rebound")[-1]["reason"] == "peers_lost"
    at(599)
    assert not launched
    at(600)
    assert launched == ["C:/qb/qbittorrent.exe"]
    assert qbit_web.count("/app/shutdown") == 1
    assert log.find("qbit_restarted")[-1]["pid"] == 4242
    at(900)
    assert log.find("qbit_heal_failed")[-1]["step"] == "restart"
    at(1500)
    assert len(log.find("qbit_heal_failed")) == 1, "one report, then wait"
    qbit_web.dht_nodes = 50
    assert at(1800)["dht_nodes"] == 50
    recovered = log.find("qbit_peers_recovered")[-1]
    assert (recovered["after"], recovered["nodes"]) == ("restart", 50)
    # Recovery resets the loop: a later loss starts from the first step.
    qbit_web.dht_nodes = 0
    at(2100)
    at(2400)
    assert len(log.find("qbit_peers_lost")) == 2
    # Nothing waiting on peers is nothing to heal.
    log2 = CapturingLog("voice")
    qbit_web.torrents = []
    idle = media_proton.ProtonPortMonitor(
        qbit, log2, path=proton_log, now=PROTON_NOW, interface="ProTUN"
    )
    for seconds in (0, 300, 600, 900):
        monkeypatch.setattr(
            idle, "now", PROTON_NOW + datetime.timedelta(seconds=seconds)
        )
        idle.reconcile_once()
    assert not log2.find("qbit_peers_lost")


EXCLUDED_UDP = """
Protocol udp Port Exclusion Ranges

Start Port    End Port
----------    --------
     50000       50059     *
     64670       64769

* - Administered port exclusions.
"""


def test_reserved_port_range_skips_administered_exclusions(monkeypatch):
    """What netsh prints on the K15. A reserved block refuses a bind; an
    administered exclusion does not, so only the first counts."""
    monkeypatch.setattr(
        media_proton,
        "_netsh",
        lambda *args: EXCLUDED_UDP if "protocol=udp" in args else "",
    )
    assert media_proton.reserved_port_range(64671) == "UDP 64670-64769"
    assert media_proton.reserved_port_range(50010) is None
    assert media_proton.reserved_port_range(41007) is None


def test_a_windows_reserved_port_names_the_cause_instead_of_restarting(
    proton_log, qbit, qbit_web, monkeypatch
):
    monkeypatch.setattr(media_proton, "PROTON_LOG_MAX_AGE_S", 10**6)
    monkeypatch.setattr(
        media_proton,
        "reserved_port_range",
        lambda port: "UDP 39700-39799" if port == 39733 else None,
    )
    log = CapturingLog("voice")
    launched = []
    qbit_web.dht_nodes = 0
    proton_monitor = media_proton.ProtonPortMonitor(
        qbit,
        log,
        path=proton_log,
        now=PROTON_NOW,
        interface="ProTUN",
        exe="C:/qb/qbittorrent.exe",
        launch=launched.append,
        sleep=lambda s: None,
    )
    for seconds in (0, 300, 600, 900):
        monkeypatch.setattr(
            proton_monitor, "now", PROTON_NOW + datetime.timedelta(seconds=seconds)
        )
        proton_monitor._tick()
    failures = log.find("proton_port_sync_failed")
    assert len(failures) == 1, "one report while it lasts"
    assert "UDP 39700-39799" in failures[0]["err"] and "39733" in failures[0]["err"]
    assert not log.find("qbit_rebound") and not launched
    assert qbit_web.count("/app/shutdown") == 0


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
    watch_log = CapturingLog("voice")
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


def test_health_watch_reaps_a_grab_that_never_starts():
    """Nothing received for the grace period gets blocklisted so the app
    takes its next candidate; a download that is moving, or complete and
    waiting to import, is left alone; a target is given up on after
    REAP_LIMIT replacements."""
    clock = [1000.0]
    dead = {
        "id": 1,
        "downloadId": "DEAD",
        "episodeId": 7,
        "title": "Show.S01E01.1080p",
        "status": "warning",
        "size": 100.0,
        "sizeleft": 100.0,
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"messages": ["stalled with no connections"]}],
    }
    magnet = {
        "id": 2,
        "downloadId": "META",
        "episodeId": 8,
        "title": "Show.S01E02.720p",
        "status": "queued",
        "size": 0.0,
        "sizeleft": 0.0,
        "trackedDownloadStatus": "ok",
        "statusMessages": [{"messages": ["qBittorrent is downloading metadata"]}],
    }
    moving = {"id": 3, "downloadId": "MOVING", "status": "downloading"}
    moving.update(size=100.0, sizeleft=40.0)
    done = {"id": 4, "downloadId": "DONE", "status": "completed"}
    done.update(size=100.0, sizeleft=0.0)
    # Idle for a reason that is not the release: not dead.
    paused = {**dead, "id": 5, "downloadId": "PAUSED", "status": "paused"}
    held = {**dead, "id": 6, "downloadId": "", "status": "delay", "size": 0.0}
    away = {
        **dead,
        "id": 7,
        "downloadId": "AWAY",
        "status": "downloadClientUnavailable",
    }
    reap_sonarr = FakeArr(
        "Sonarr",
        queue={"records": [dead, magnet, moving, done, paused, held, away]},
    )
    reap_log = CapturingLog("voice")
    watch = media_health.MediaHealthMonitor(
        (reap_sonarr,), reap_log, stall_grace_s=1800, now=lambda: clock[0]
    )
    watch.reconcile_once()
    clock[0] += 1799
    watch.reconcile_once()
    assert not reap_sonarr.deletes
    clock[0] += 1
    watch.reconcile_once()
    blocklist = {"removeFromClient": "true", "blocklist": "true"}
    assert reap_sonarr.deletes == [("queue/1", blocklist), ("queue/2", blocklist)]
    reaped = reap_log.find("media_queue_reaped")
    assert [(r["download"], r["idle_s"], r["attempt"]) for r in reaped] == [
        ("DEAD", 1800, 1),
        ("META", 1800, 1),
    ]
    # The app grabs the next copy for the same episode: a fresh clock, and
    # the target's count carries on until the limit.
    for n, hash_ in enumerate(("DEAD2", "DEAD3", "DEAD4"), start=2):
        reap_sonarr.queue["records"] = [{**dead, "id": 10 + n, "downloadId": hash_}]
        watch.reconcile_once()
        clock[0] += 1800
        watch.reconcile_once()
    assert [r["attempt"] for r in reap_log.find("media_queue_reaped")][2:] == [2, 3]
    assert len(reap_sonarr.deletes) == 4, "the fourth copy is left where it is"
    failed = reap_log.find("media_queue_reap_failed")
    assert len(failed) == 1 and "3 dead grabs" in failed[0]["err"]
    clock[0] += 1800
    watch.reconcile_once()
    assert len(reap_log.find("media_queue_reap_failed")) == 1, "said once"
    # Off means off; the stall is still reported.
    off_sonarr = FakeArr("Sonarr", queue={"records": [dict(dead)]})
    off_log = CapturingLog("voice")
    off = media_health.MediaHealthMonitor(
        (off_sonarr,), off_log, stall_grace_s=0, now=lambda: clock[0]
    )
    off.reconcile_once()
    clock[0] += 10**6
    off.reconcile_once()
    assert not off_sonarr.deletes and off_log.find("media_queue_stalled")


def test_health_watch_reaps_a_download_that_completed_empty():
    """qBittorrent's excluded-file-names filter deselects the executable in a
    fake release, so the download finishes having transferred nothing and the
    app reports no eligible files rather than the executable verdict. Final on
    sight, like the executable verdict; a real completed grab still waits."""
    clock = [1000.0]
    empty = {
        "id": 1,
        "downloadId": "EMPTY",
        "episodeId": 435,
        "title": "Show.S18E07.1080p.WEB.h264-FAKE.exe",
        "status": "completed",
        "size": 0.0,
        "sizeleft": 0.0,
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [
            {"messages": ["No files found are eligible for import in /data/torrents"]}
        ],
    }
    importing = {
        "id": 2,
        "downloadId": "REAL",
        "episodeId": 436,
        "status": "completed",
        "size": 100.0,
        "sizeleft": 0.0,
    }
    sonarr = FakeArr("Sonarr", queue={"records": [empty, importing]})
    log = CapturingLog("voice")
    watch = media_health.MediaHealthMonitor(
        (sonarr,), log, stall_grace_s=1800, now=lambda: clock[0]
    )
    watch.reconcile_once()
    blocklist = {"removeFromClient": "true", "blocklist": "true"}
    assert sonarr.deletes == [("queue/1", blocklist)]
    reaped = log.find("media_queue_reaped")
    assert [(r["download"], r["reason"], r["idle_s"]) for r in reaped] == [
        ("EMPTY", "empty", 0)
    ]


# --- grabs no operation asked for ---------------------------------------------


@dataclasses.dataclass
class FakeLedger:
    rows: list

    def active(self, kind=None):
        return list(self.rows)


def test_unattributed_grabs_are_reported_per_download():
    grab_sonarr = FakeArr("Sonarr")
    watch_log = CapturingLog("voice")
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
    watch_log = CapturingLog("voice")
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
Usage = collections.namedtuple("Usage", "total free")


def test_disk_watch_reports_crossings(monkeypatch):
    table = {"M:": Usage(1000 * GB, 100 * GB), "C:": Usage(1000 * GB, 900 * GB)}

    def disk_usage(mount):
        if isinstance(table[mount], Exception):
            raise table[mount]
        return table[mount]

    monkeypatch.setattr(disk_health.shutil, "disk_usage", disk_usage)
    disk_log = CapturingLog("voice")
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


@pytest.mark.parametrize(
    "factory, flag",
    [
        (
            lambda cfg, log: media.media_health_monitor_from_config(cfg, {}, log),
            "healthSync",
        ),
        (lambda cfg, log: media.disk_health_monitor_from_config(cfg, log), "diskWatch"),
    ],
)
def test_a_watch_is_off_when_config_says_so(factory, flag):
    cfg = {"media": {"enabled": True, flag: False}}
    assert factory(cfg, CapturingLog("voice")) is None


def test_health_watch_refuses_a_bad_stall_grace_without_falling_over():
    """A bad value disables the watch with a warning the doctor shows, the
    way every other media key does, rather than taking the lane down."""
    cfg = {"media": {"enabled": True, "stalledGraceMinutes": "soon"}}
    log = CapturingLog("voice")
    assert media.media_health_monitor_from_config(cfg, {}, log) is None
    assert "stalledGraceMinutes" in log.find("lane_disabled")[-1]["reason"]


def test_health_watch_refuses_a_missing_arr_url_the_same_way():
    """A missing URL is a MediaConfigurationError from the shared client
    builder; the watch factories catch nothing else."""
    cfg = {"media": {"enabled": True, "sonarrUrl": "http://s"}}
    secrets = {"radarrApiKey": "k" * 32, "sonarrApiKey": "k" * 32}
    log = CapturingLog("voice")
    assert media.media_health_monitor_from_config(cfg, secrets, log) is None
    assert log.find("lane_disabled")[-1]["reason"] == "media.radarrUrl is missing"


def test_disk_watch_needs_a_host_root():
    # No media/.env in the runtime home means no host root to resolve: a
    # checkout that is not the K15 runs the supervisor without inventing a
    # volume to watch.
    cfg = {"media": {"enabled": True}}
    assert media.disk_health_monitor_from_config(cfg, CapturingLog("voice")) is None


# --- factory gating -----------------------------------------------------------


def test_from_config_needs_the_lane_and_its_keys():
    cfg = json.loads(json.dumps(helpers.CONFIG))
    log = CapturingLog("voice")
    assert media.from_config(cfg, {}, log) is None
    cfg["media"]["enabled"] = True
    assert media.from_config(cfg, {}, log) is None
    assert log.find("lane_disabled")[-1]["what"] == "media"
    # With the two arr keys the lane is up; Prowlarr and qBittorrent are
    # extras whose absence disables only their own tools.
    keys = {"radarrApiKey": "r" * 32, "sonarrApiKey": "s" * 32}
    svc = media.from_config(cfg, keys, log)
    assert svc is not None and svc.prowlarr is None and svc.qbit is None
    assert {r["what"] for r in log.find("lane_disabled")} >= {
        "prowlarr_tools",
        "torrent_tools",
    }
    full = media.from_config(
        cfg,
        {**keys, "prowlarrApiKey": "p" * 32, "qbittorrentPassword": "q" * 16},
        log,
    )
    assert full.prowlarr.api_version == "v1" and full.qbit is not None


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
        log = CapturingLog("voice")

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
    "excluded_file_names_enabled": True,
    "excluded_file_names": "*.exe\n*.scr\n*.bat",
}


def _doctor(monkeypatch, tmp_path, qbit_preferences, dht_nodes=120):
    """media_doctor over a stack whose only variables are what qBittorrent
    reports for its preferences and its DHT."""
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
        if path.endswith("/transfer/info"):
            return {}, json.dumps({"dht_nodes": dht_nodes}).encode()
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
    monkeypatch.setattr(media_clients, "_http_transport", doctor_arr_transport)
    monkeypatch.setattr(media_clients, "_qbit_http_transport", doctor_qbit_transport)
    monkeypatch.setattr(
        media_checks, "_compose_services", lambda media_dir: compose_rows
    )
    monkeypatch.setattr(
        media_proton, "default_proton_log_path", lambda: doctor_proton_log
    )
    return media_checks.media_doctor(
        doctor_cfg,
        DOCTOR_SECRETS,
        now=datetime.datetime(2026, 8, 30, 5, 0, 5, tzinfo=UTC),
    )


def test_media_doctor_passes_a_healthy_stack(monkeypatch, tmp_path):
    doctor = _doctor(monkeypatch, tmp_path, dict(HEALTHY_QBIT_PREFERENCES))
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


def test_media_doctor_fails_a_misconfigured_qbittorrent(monkeypatch, tmp_path):
    broken_preferences = dict(
        HEALTHY_QBIT_PREFERENCES,
        current_network_interface="Ethernet",
        upnp=True,
        share_limits_mode="MatchAll",
        listen_port=1234,
        excluded_file_names_enabled=False,
    )
    broken = _doctor(monkeypatch, tmp_path, broken_preferences, dht_nodes=0)
    assert not broken["ok"]
    assert any(
        row["name"] == "qBittorrent UPnP/NAT-PMP" and row["level"] == "FAIL"
        for row in broken["checks"]
    )
    assert any(
        row["name"] == "qBittorrent DHT" and row["level"] == "FAIL"
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
    assert any(
        row["name"] == "qBittorrent excluded file names" and row["level"] == "FAIL"
        for row in broken["checks"]
    )


# --- app updates --------------------------------------------------------------


class FakeServarr:
    """One app's status and release list. Each status read takes the next
    entry of `statuses`; None is the app not answering, as mid-restart."""

    name = "Radarr"

    def __init__(self, statuses, releases=()):
        self.statuses = list(statuses)
        self.releases = list(releases)

    def get(self, endpoint, params=None):
        if endpoint == "update":
            return list(self.releases)
        if endpoint == "queue":
            return self.queue
        assert endpoint == "system/status"
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise media_clients.MediaError("media service is unreachable")
        return status


RADARR_6_3 = {"version": "6.3.0.10514", "packageVersion": "6.3.0.10514-ls314"}
RADARR_6_4 = {"version": "6.4.4.10685", "packageVersion": "6.4.4.10685-ls320"}


def test_available_update_names_a_newer_release_only():
    releases = [
        {
            "version": "6.4.4.10685",
            "latest": True,
            "installed": False,
            "releaseDate": "2026-09-16T18:13:40Z",
        },
        {"version": "6.3.0.10514", "latest": False, "installed": True},
    ]
    assert media_updates.available_update(FakeServarr([RADARR_6_3], releases)) == {
        "installed": "6.3.0.10514",
        "latest": "6.4.4.10685",
        "released": "2026-09-16",
    }
    current = [dict(releases[0], installed=True)]
    assert media_updates.available_update(FakeServarr([RADARR_6_4], current)) is None


class FakeDocker:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command[command.index("--env-file") + 2 :])
        return subprocess.CompletedProcess(command, self.returncode, "", "denied")


def _media_dir(tmp_path):
    (tmp_path / ".env").write_text("MEDIA_ROOT=media", encoding="utf-8")
    return tmp_path


def test_update_recreates_one_container_and_waits_for_the_app(tmp_path):
    docker = FakeDocker()
    app = FakeServarr([RADARR_6_3, None, None, RADARR_6_4])
    result = media_updates.update_app(
        app, _media_dir(tmp_path), run=docker, now=lambda: 0, sleep=lambda s: None
    )
    assert docker.commands == [["pull", "radarr"], ["up", "-d", "radarr"]]
    assert result == {
        "app": "Radarr",
        "before": "6.3.0.10514-ls314",
        "after": "6.4.4.10685-ls320",
    }


def test_update_fails_on_a_refused_pull_or_an_app_that_never_returns(tmp_path):
    with pytest.raises(media_clients.MediaError, match="denied"):
        media_updates.update_app(
            FakeServarr([RADARR_6_3]), _media_dir(tmp_path), run=FakeDocker(1)
        )
    clock = iter(range(0, 1000, 60))
    with pytest.raises(media_clients.MediaError, match="did not answer"):
        media_updates.update_app(
            FakeServarr([RADARR_6_3, None]),
            _media_dir(tmp_path),
            run=FakeDocker(),
            now=lambda: next(clock),
            sleep=lambda s: None,
        )


def _night_watch(apps, update, hour=4):
    log = CapturingLog()
    watch = media_updates.MediaUpdateMonitor(
        apps,
        log,
        "media",
        update=update,
        clock=lambda: datetime.datetime(2026, 9, 27, hour, 5),
    )
    watch.reconcile_once()
    return log


def _offered(app, installed, latest, queue=()):
    server = FakeServarr(
        [{"version": installed}], [{"version": latest, "latest": True}]
    )
    server.name = app
    server.queue = {"records": list(queue)}
    return server


def test_update_watch_applies_minors_overnight_and_holds_majors():
    updated = []

    def update(client, media_dir):
        updated.append(client.name)
        return {"app": client.name, "before": "6.3.0-ls314", "after": "6.4.4-ls320"}

    radarr = _offered("Radarr", "6.3.0", "6.4.4")
    sonarr = _offered("Sonarr", "4.0.19", "5.0.0")
    assert _night_watch([radarr, sonarr], update, hour=15).records == []
    log = _night_watch([radarr, sonarr], update)
    assert updated == ["Radarr"]
    assert log.find("media_update_applied")[0]["after"] == "6.4.4-ls320"
    assert log.find("media_update_held")[0]["latest"] == "5.0.0"


def test_update_watch_waits_out_an_import_and_tells_a_failed_update_from_a_skip():
    def update(client, media_dir):
        raise media_clients.MediaError("pull access denied")

    importing = _offered(
        "Radarr", "6.3.0", "6.4.4", [{"trackedDownloadState": "importing"}]
    )
    down = FakeServarr([None], [{"version": "4.0.20", "latest": True}])
    down.name = "Sonarr"
    prowlarr = _offered("Prowlarr", "2.5.2", "2.6.5")
    log = _night_watch([importing, down, prowlarr], update)
    assert [(r["event"], r["app"]) for r in log.records] == [
        ("media_update_skipped", "Sonarr"),
        ("media_update_failed", "Prowlarr"),
    ]
