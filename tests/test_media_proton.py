"""Proton's forwarded port: reading its client log, holding qBittorrent's
listening port to it, and healing dead peer sockets."""

import datetime

import pytest

from helpers import CapturingLog, FakeQbitWeb
from slopstation.agent.media import clients, proton

UTC = datetime.UTC


@pytest.fixture
def qbit_web():
    return FakeQbitWeb()


@pytest.fixture
def qbit(qbit_web):
    return clients.QbittorrentClient(
        "http://127.0.0.1:8080",
        "admin",
        "a-long-qbit-password",
        transport=qbit_web.transport,
    )


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
    source = proton.read_proton_port_state(proton_log, now=PROTON_NOW)
    assert source["state"] == "active" and source["port"] == 39733
    assert (
        proton.read_proton_port_state(tmp_path / "missing.txt", now=PROTON_NOW)["state"]
        == "missing"
    )
    proton_log.write_text("not a Proton status line", encoding="utf-8")
    assert (
        proton.read_proton_port_state(proton_log, now=PROTON_NOW)["state"] == "unknown"
    )
    proton_backup = tmp_path / "client-logs.1.txt"
    proton_backup.write_text(
        proton_event("2026-08-30T04:12:03.000Z", "SleepingUntilRefresh", 40123),
        encoding="utf-8",
    )
    rotated = proton.read_proton_port_state(
        proton_log, now=datetime.datetime(2026, 8, 30, 4, 12, 4, tzinfo=UTC)
    )
    assert rotated["state"] == "active" and rotated["port"] == 40123


def test_proton_monitor_syncs_a_fresh_mapping(proton_log, qbit, qbit_web, monkeypatch):
    qbit_web.preferences["listen_port"] = 33125
    proton_monitor = proton.ProtonPortMonitor(
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
    with pytest.raises(clients.MediaError, match="stale"):
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
    proton_monitor = proton.ProtonPortMonitor(
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
    monkeypatch.setattr(proton, "PROTON_LOG_MAX_AGE_S", 10**6)
    monkeypatch.setattr(proton, "reserved_port_range", lambda port: None)
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
    proton_monitor = proton.ProtonPortMonitor(
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
    idle = proton.ProtonPortMonitor(
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
        proton,
        "_netsh",
        lambda *args: EXCLUDED_UDP if "protocol=udp" in args else "",
    )
    assert proton.reserved_port_range(64671) == "UDP 64670-64769"
    assert proton.reserved_port_range(50010) is None
    assert proton.reserved_port_range(41007) is None


def test_a_windows_reserved_port_names_the_cause_instead_of_restarting(
    proton_log, qbit, qbit_web, monkeypatch
):
    monkeypatch.setattr(proton, "PROTON_LOG_MAX_AGE_S", 10**6)
    monkeypatch.setattr(
        proton,
        "reserved_port_range",
        lambda port: "UDP 39700-39799" if port == 39733 else None,
    )
    log = CapturingLog("voice")
    launched = []
    qbit_web.dht_nodes = 0
    proton_monitor = proton.ProtonPortMonitor(
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
