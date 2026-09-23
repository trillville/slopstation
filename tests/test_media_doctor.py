"""The doctor's media rows over a faked stack: the three apps over HTTP,
qBittorrent's Web UI, Docker Compose, Proton's client log and netsh."""

import datetime
import json
import time
import urllib.parse

import pytest

import helpers
from slopstation import paths, statefile
from slopstation.agent.media import clients, doctor, proton

NOW = datetime.datetime(2026, 8, 30, 5, 0, 5, tzinfo=datetime.UTC)
SECRETS = {
    "radarrApiKey": "radarr-key-long-enough",
    "sonarrApiKey": "sonarr-key-long-enough",
    "prowlarrApiKey": "prowlarr-key-long-enough",
    "qbittorrentPassword": "qbit-password-long-enough",
}
HEALTHY_QBIT = {
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
# Proton mapped 33125 five seconds before NOW.
PROTON_LOG = (
    "2026-08-30T05:00:00.000Z | INFO  | PROCESS.COMM | Received PortForwarding "
    "Status 'SleepingUntilRefresh' triggered at 'fixture', Port pair "
    "33125->33125, expiring in 00:01:00 |\n"
    '{"Caller":"ClientControllerListener"}\n'
)


class Rows(list):
    """Every report() call as (level, name, detail, hint)."""

    def report(self, level, name, detail, hint=""):
        self.append((level, name, detail, hint))

    def levels(self):
        return {name: level for level, name, _, _ in self}

    def detail(self, name):
        return next(detail for _, n, detail, _ in self if n == name)


class Stack:
    """A healthy stack. A test changes what it needs, then runs the rows."""

    def __init__(self, monkeypatch, tmp_path):
        self.cfg = json.loads(json.dumps(helpers.CONFIG))
        self.cfg["media"].update(enabled=True, protonPortSync=True)
        self.secrets = dict(SECRETS)
        self.qbit = dict(HEALTHY_QBIT)
        self.dht_nodes = 120
        self.wanted: list = []
        self.dynamic = (21000, 11000)  # netsh's first port and count
        log = tmp_path / "client-logs.txt"
        log.write_text(PROTON_LOG, encoding="utf-8")
        compose = [
            {"Service": name, "State": "running", "Health": ""}
            for name in doctor.CONTAINERS
        ]
        monkeypatch.setattr(clients, "_http_transport", self._arr)
        monkeypatch.setattr(clients, "_qbit_http_transport", self._qbit)
        monkeypatch.setattr(doctor, "_compose_services", lambda media_dir: compose)
        monkeypatch.setattr(proton, "default_proton_log_path", lambda: log)
        monkeypatch.setattr(proton, "_netsh", self._netsh)

    def run(self):
        rows = Rows()
        doctor.check(self.cfg, self.secrets, rows.report, now=NOW)
        return rows

    def _arr(self, method, url, headers, body, timeout):
        split = urllib.parse.urlsplit(url)
        path, media = split.path, self.cfg["media"]
        movies = split.port == 7878
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
                for name in media["managedIndexers"]
            ]
        if path.endswith("/api/v1/applications"):
            return [
                {"name": name, "implementation": name, "syncLevel": "fullSync"}
                for name in ("Radarr", "Sonarr")
            ]
        if path.endswith("/rootfolder"):
            return [{"path": media["movieRoot" if movies else "seriesRoot"]}]
        if path.endswith("/qualityprofile"):
            presets = media["moviePresets" if movies else "seriesPresets"]
            return [{"name": name} for name in set(presets.values())]
        if path.endswith("/indexer"):
            return [{"name": "synced", "enable": True}]
        if path.endswith("/config/downloadclient"):
            return {"enableCompletedDownloadHandling": True}
        if path.endswith("/downloadclient"):
            field = "movieCategory" if movies else "tvCategory"
            return [
                {
                    "implementation": "QBittorrent",
                    "enable": True,
                    "removeCompletedDownloads": True,
                    "fields": [
                        {"name": field, "value": "radarr" if movies else "sonarr"}
                    ],
                }
            ]
        if path.endswith("/wanted/missing"):
            return {"records": self.wanted}
        raise AssertionError((method, url))

    def _qbit(self, method, url, headers, body, timeout):
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/auth/login"):
            return {"Set-Cookie": "SID=doctor; path=/"}, b"Ok."
        if path.endswith("/app/version"):
            return {}, b"5.1.4"
        if path.endswith("/app/preferences"):
            return {}, json.dumps(self.qbit).encode()
        if path.endswith("/torrents/categories"):
            return {}, json.dumps({"radarr": {}, "sonarr": {}}).encode()
        if path.endswith("/transfer/info"):
            return {}, json.dumps({"dht_nodes": self.dht_nodes}).encode()
        raise AssertionError((method, url))

    def _netsh(self, *args):
        start, count = self.dynamic
        return (
            f"Protocol {args[-1]} Dynamic Port Range\n"
            "---------------------------------\n"
            f"Start Port      : {start}\n"
            f"Number of Ports : {count}\n"
        )


@pytest.fixture
def stack(monkeypatch, tmp_path):
    return Stack(monkeypatch, tmp_path)


def test_a_disabled_stack_is_one_row(stack):
    stack.cfg["media"]["enabled"] = False
    assert stack.run() == [("PASS", "media", "disabled", "")]


def test_a_healthy_stack_passes_every_row(stack):
    rows = stack.run()
    assert {level for level, *_ in rows} == {"PASS"}, rows
    assert {
        "media config",
        "Docker media containers",
        "Radarr quality profiles",
        "Sonarr qBittorrent client",
        "Prowlarr radarr sync",
        "qBittorrent excluded file names",
        "Proton port synchronization",
        "port reservations",
        "media monitoring",
    } <= set(rows.levels())


def test_a_misconfigured_qbittorrent_warns_on_each_setting(stack):
    stack.qbit.update(
        upnp=True,
        share_limits_mode="MatchAll",
        listen_port=1234,
        excluded_file_names_enabled=False,
    )
    stack.dht_nodes = 0
    levels = stack.run().levels()
    for name in (
        "qBittorrent UPnP/NAT-PMP",
        "qBittorrent DHT",
        "qBittorrent share-limit mode",
        "qBittorrent excluded file names",
        "Proton port synchronization",
    ):
        assert levels[name] == "WARN", name


def test_a_missing_setting_names_itself_and_skips_what_needs_it(stack):
    del stack.cfg["media"]["prowlarrUrl"]
    del stack.secrets["sonarrApiKey"]
    rows = stack.run()
    levels = rows.levels()
    assert "prowlarrUrl" in rows.detail("media config")
    assert levels["Prowlarr API"] == "WARN"
    sonarr = next(row for row in rows if row[1] == "Sonarr API")
    assert sonarr == (
        "WARN",
        "Sonarr API",
        "sonarrApiKey is missing",
        "copy it from Sonarr > Settings > General into secrets.json",
    )
    # Nothing that needs Sonarr's answers runs without them.
    assert "Sonarr root" not in levels
    assert levels["media monitoring"] == "WARN"
    assert levels["Radarr root"] == "PASS"


def test_port_reservations_warn_while_the_dynamic_range_reaches_proton(stack):
    """Windows reserves ports only inside its dynamic range, so the row
    passes once that range ends below Proton's forwarded ports."""
    stack.dynamic = (58000, 7536)
    rows = stack.run()
    assert rows.levels()["port reservations"] == "WARN"
    assert "UDP 58000-65535" in rows.detail("port reservations")
    stack.dynamic = (21000, 11000)
    assert stack.run().levels()["port reservations"] == "PASS"


def _wanted_missing():
    """Sonarr's wanted/missing records: four monitored episodes, one per case
    the row tells apart."""
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400))
    tonight = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return [
        # Owned by the Andor operation the tests record: not drift.
        {
            "seriesId": 7,
            "seasonNumber": 1,
            "airDateUtc": old,
            "series": {"title": "Andor"},
        },
        # Aired tonight: still in flight, not drift.
        {
            "seriesId": 3,
            "seasonNumber": 18,
            "airDateUtc": tonight,
            "series": {"title": "Sunny"},
        },
        # Aired long ago, nothing chasing it: the surprise-download hole.
        {
            "seriesId": 3,
            "seasonNumber": 1,
            "airDateUtc": old,
            "series": {"title": "Sunny"},
        },
        {
            "seriesId": 3,
            "seasonNumber": 2,
            "airDateUtc": old,
            "series": {"title": "Sunny"},
        },
    ]


def _series_op(external_ref, seasons):
    return {
        "kind": "series_acquisition",
        "state": "RUNNING",
        "external_ref": external_ref,
        "metadata": {"seasons": seasons},
    }


def test_monitoring_flags_episodes_nobody_is_chasing(stack):
    stack.wanted = _wanted_missing()
    statefile.write(paths.state() / "operations.json", [_series_op("7", [1])])
    rows = stack.run()
    assert rows.levels()["media monitoring"] == "WARN"
    detail = rows.detail("media monitoring")
    assert "2 episode(s)" in detail and "Sunny (2)" in detail, detail


def test_monitoring_counts_a_whole_series_operation_as_every_season(stack):
    # A whole-series operation (seasons: null) accounts for every season of
    # it, so with both series owned nothing is left armed.
    stack.wanted = _wanted_missing()
    statefile.write(
        paths.state() / "operations.json",
        [_series_op("7", [1]), _series_op("3", None)],
    )
    assert stack.run().levels()["media monitoring"] == "PASS"
