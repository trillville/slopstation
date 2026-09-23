"""The Radarr and Sonarr client's request shape, and qBittorrent's Web UI
client: login, form fields, and rebinding to the VPN adapter."""

import json
import urllib.parse

import pytest

from helpers import FakeQbitWeb
from slopstation.agent.media import clients


def test_arr_client_sends_the_key_and_encodes_the_body():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        return {"ok": True}

    client = clients.ArrClient(
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
    client.get("release", {"episodeId": 9}, timeout=clients.SEARCH_TIMEOUT_S)
    assert [c[4] for c in calls] == [
        clients.HTTP_TIMEOUT_S,
        clients.HTTP_TIMEOUT_S,
        clients.SEARCH_TIMEOUT_S,
    ]


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
    with pytest.raises(clients.MediaError):
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
    with pytest.raises(clients.MediaError, match="no network interface"):
        qbit.rebind_interface("Ethernet")
