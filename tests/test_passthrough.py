"""Test the direct API tools: the gate, the blocklist, the scrub, the docs."""

import json
import time
import types

import pytest

from helpers import CapturingLog
from slopstation import paths
from slopstation.agent.llm import assistant, confirm
from slopstation.agent.llm.toolsets import passthrough
from slopstation.agent.tools import apidocs


class FakeClient:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer if answer is not None else {"ok": 1}

    def call(self, method, endpoint, params=None, payload=None):
        self.calls.append((method, endpoint, params, payload))
        return self.answer


@pytest.fixture
def log():
    return CapturingLog("voice")


@pytest.fixture
def live(log):
    """A live (not dry-run) Toolkit over fake clients, inside one utterance."""
    dispatch = types.SimpleNamespace(
        dry_run=False,
        utterance=types.SimpleNamespace(turn="aa0001", asked="pause the dune torrent"),
    )
    media = types.SimpleNamespace(
        cfg={"radarrUrl": "http://r:7878"},
        radarr=FakeClient({"apiKey": "SECRET", "rows": [{"title": "Dune"}]}),
        sonarr=FakeClient(),
        prowlarr=FakeClient(),
        qbit=FakeClient(
            [
                {
                    "name": "x",
                    "tracker": "udp://t.example/announce/abc123",
                    "url": "https://ok.example/",
                }
            ]
        ),
    )
    tk = assistant.Toolkit(dispatch, log, media=media)
    tk.load(assistant.REGISTRY.names())
    return tk, dispatch, media


def test_reads_run_at_once_scrubbed_capped_and_logged(live, log):
    tk, _, media = live
    out = tk.call(
        "radarr_api", {"method": "GET", "path": "/movie", "params": {"tmdbId": 1}}
    )
    assert out["ok"] and media.radarr.calls == [("GET", "movie", {"tmdbId": 1}, None)]
    assert (
        out["result"]["apiKey"] == "[redacted]"
        and out["result"]["rows"][0]["title"] == "Dune"
    )
    gap = log.find("tool_gap")[-1]
    assert (gap["api"], gap["method"], gap["path"]) == ("radarr", "GET", "movie")
    assert gap["asked"] == "pause the dune torrent"
    # Tracker URLs carry private-tracker passkeys: redacted; other URLs stay.
    q = tk.call("qbittorrent_api", {"method": "GET", "path": "torrents/info"})
    assert q["result"][0]["tracker"] == "[redacted tracker url]"
    assert q["result"][0]["url"] == "https://ok.example/"
    # A huge answer is cut, and says so.
    media.sonarr.answer = [{"i": i} for i in range(5000)]
    big = tk.call("sonarr_api", {"method": "GET", "path": "series"})
    assert big["ok"] and big["truncated"] and len(big["result"]) < 9000


def test_mutations_wait_for_a_confirmation_from_a_later_turn(live, log, monkeypatch):
    tk, dispatch, media = live
    ask = {
        "method": "POST",
        "path": "command",
        "body": {"name": "MoviesSearch", "movieIds": [12]},
    }
    first = tk.call("radarr_api", dict(ask))
    assert not first["ok"] and first["confirm"].startswith("POST /command")
    assert '"MoviesSearch"' in first["confirm"] and media.radarr.calls == []
    # Same turn again: still refused - the model cannot answer itself.
    assert not tk.call("radarr_api", dict(ask))["ok"]
    assert log.find("tool_refused")[-1]["reason"] == "unconfirmed"
    # A later turn with the identical request commits.
    dispatch.utterance = types.SimpleNamespace(turn="aa0002", asked="yes")
    done = tk.call("radarr_api", dict(ask))
    assert done["ok"] and media.radarr.calls[-1][0] == "POST"
    # A DIFFERENT body is a new question, not a confirmed one.
    other = dict(ask, body={"name": "MoviesSearch", "movieIds": [13]})
    dispatch.utterance = types.SimpleNamespace(turn="aa0003", asked="and 13")
    assert not tk.call("radarr_api", other)["ok"]
    # A stale ask expires.
    monkeypatch.setattr(confirm, "ASK_TTL_S", -1)
    dispatch.utterance = types.SimpleNamespace(turn="aa0004", asked="yes")
    assert not tk.call("radarr_api", other)["ok"]


def test_the_blocklist_and_the_shape_checks_refuse_outright(live, log):
    tk, _, media = live
    for service, call in (
        ("radarr_api", {"method": "PUT", "path": "config/host", "body": {}}),
        ("radarr_api", {"method": "GET", "path": "config/host"}),
        ("sonarr_api", {"method": "DELETE", "path": "indexer/3"}),
        ("prowlarr_api", {"method": "POST", "path": "applications", "body": {}}),
        ("qbittorrent_api", {"method": "POST", "path": "app/shutdown"}),
        (
            "qbittorrent_api",
            {"method": "POST", "path": "app/setPreferences", "body": {"json": "{}"}},
        ),
    ):
        out = tk.call(service, call)
        assert not out["ok"] and "operator setting" in out["error"], (service, call)
    assert log.find("tool_refused")[-1]["reason"] == "blocklisted"
    # Prowlarr reads on indexers are fine: the blocklist is on writes.
    assert tk.call("prowlarr_api", {"method": "GET", "path": "indexer"})["ok"]
    assert not tk.call("radarr_api", {"method": "PATCH", "path": "movie"})["ok"]
    assert not tk.call("radarr_api", {"method": "GET", "path": "../secrets"})["ok"]
    assert not tk.call("radarr_api", {"method": "GET", "path": "movie", "params": [1]})[
        "ok"
    ]
    assert media.radarr.calls == []


def test_dry_run_reports_a_mutation_without_sending_it(log):
    dispatch = types.SimpleNamespace(
        dry_run=True, utterance=types.SimpleNamespace(turn="aa0001", asked="")
    )
    media = types.SimpleNamespace(
        cfg={}, radarr=FakeClient(), sonarr=None, prowlarr=None, qbit=FakeClient()
    )
    tk = assistant.Toolkit(dispatch, log, media=media)
    tk.load(["radarr_api", "qbittorrent_api"])
    out = tk.call(
        "radarr_api", {"method": "POST", "path": "command", "body": {"name": "RssSync"}}
    )
    assert (
        out["dry_run"] and "POST /command" in out["detail"] and media.radarr.calls == []
    )
    # qBittorrent GETs fold body fields into the query, since its API is form-shaped.
    tk.call(
        "qbittorrent_api",
        {"method": "GET", "path": "torrents/info", "body": {"filter": "paused"}},
    )
    assert media.qbit.calls[-1] == ("GET", "torrents/info", {"filter": "paused"}, None)


def test_offered_only_with_the_matching_service(log):
    dispatch = types.SimpleNamespace(dry_run=True, utterance=None)
    bare = assistant.Toolkit(dispatch, log)
    assert "steam_api" in bare.offered and "radarr_api" not in bare.offered
    without_qbit = types.SimpleNamespace(
        cfg={}, radarr=FakeClient(), sonarr=FakeClient(), prowlarr=None, qbit=None
    )
    tk = assistant.Toolkit(dispatch, log, media=without_qbit)
    assert {"radarr_api", "sonarr_api", "describe_api"} <= set(tk.offered)
    assert "qbittorrent_api" not in tk.offered and "prowlarr_api" not in tk.offered
    # Passthrough tools are never default: find_tools is the way in.
    assert not any(
        assistant.REGISTRY.get(n).default for n in ("radarr_api", "steam_api")
    )


def test_steam_api_injects_the_right_credential(live, monkeypatch):
    tk, _, _ = live
    sent = []

    class R:
        status_code = 200
        headers = {"X-eresult": "1"}
        text = ""

        def json(self):
            return {"response": {}}

    fake_requests = types.SimpleNamespace(
        request=lambda m, url, **kw: sent.append((m, url, kw)) or R()
    )
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)
    monkeypatch.setattr(
        "slopstation.agent.tools.library.steam_creds", lambda: ("KEY123", "7656119")
    )
    out = tk.call(
        "steam_api",
        {
            "method": "GET",
            "path": "ISteamUserStats/GetPlayerAchievements/v1",
            "auth": "key",
            "params": {"appid": 1},
        },
    )
    assert (
        out["ok"]
        and sent[-1][1]
        == "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
    )
    assert sent[-1][2]["params"] == {"appid": 1, "key": "KEY123", "steamid": "7656119"}
    # The credential never comes back in the result.
    assert "KEY123" not in json.dumps(out)
    # Store paths go to the store host, keyless.
    tk.call(
        "steam_api",
        {"method": "GET", "path": "store.steampowered.com/api/featuredcategories"},
    )
    assert sent[-1][1] == "https://store.steampowered.com/api/featuredcategories"
    assert "key" not in sent[-1][2]["params"]
    # An account call without an enrolled session fails plainly.
    out = tk.call(
        "steam_api",
        {
            "method": "GET",
            "path": "IClientCommService/GetClientAppList/v1",
            "auth": "account",
        },
    )
    assert not out["ok"] and "not enrolled" in out["error"]
    assert not tk.call("steam_api", {"method": "GET", "path": "x", "auth": "magic"})[
        "ok"
    ]


# --- describe_api -------------------------------------------------------------

OPENAPI = {
    "paths": {
        "/api/v3/queue": {
            "get": {"summary": "queue", "parameters": [{"name": "page", "in": "query"}]}
        },
        "/api/v3/queue/{id}": {
            "delete": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True},
                    {"name": "removeFromClient", "in": "query"},
                ]
            }
        },
        "/api/v3/release": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ReleaseResource"}
                        }
                    }
                }
            }
        },
    },
    "components": {
        "schemas": {
            "ReleaseResource": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "indexerId": {"type": "integer"},
                },
            }
        }
    },
}


def test_describe_api_slices_openapi_and_caches_for_a_day(tmp_path, monkeypatch):
    fetched = []

    def opener(url):
        fetched.append(url)
        if "127.0.0.1" in url:
            raise OSError("app does not serve docs")
        return json.dumps(OPENAPI)

    out = apidocs.describe("radarr", "queue", "http://127.0.0.1:7878", opener)
    # The live URL is tried first, the project's copy second.
    assert (
        fetched[0].startswith("http://127.0.0.1:7878/docs/v3/")
        and "githubusercontent" in fetched[1]
    )
    doc = json.loads(out["doc"])
    assert set(doc) == {"/api/v3/queue", "/api/v3/queue/{id}"} and out["paths"] == 2
    assert doc["/api/v3/queue/{id}"]["DELETE"]["params"] == [
        "id:path!",
        "removeFromClient:query",
    ]
    release = json.loads(apidocs.describe("radarr", "release", None, opener)["doc"])
    assert release["/api/v3/release"]["POST"]["body"] == {
        "guid": "string",
        "indexerId": "integer",
    }
    # Cached: the second describe fetched nothing more.
    assert len(fetched) == 2 and paths.state("apidocs-radarr.txt").exists()
    monkeypatch.setattr(apidocs, "CACHE_S", -1)
    apidocs.describe("radarr", "queue", None, opener)
    assert len(fetched) == 3


def test_describe_api_slices_the_wiki_by_section(monkeypatch):
    wiki = "# Intro\n\nwords\n\n# Torrent management\n\n## Get torrent list\n\nGET torrents/info filter=\n\n## Pause torrents\n\nPOST torrents/stop hashes=\n\n# Transfer info\n\nGET transfer/info"
    out = apidocs.describe("qbittorrent", "pause", None, lambda url: wiki)
    assert out["sections"] == 1 and "torrents/stop" in out["doc"]
    both = apidocs.describe("qbittorrent", "torrents/", None, lambda url: wiki)
    assert both["sections"] == 2 and both["doc"].index("Get torrent list") < both[
        "doc"
    ].index("Pause")


def test_describe_api_tool_reports_a_fetch_failure(live, monkeypatch):
    tk, _, _ = live
    monkeypatch.setattr(
        apidocs, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    out = tk.call("describe_api", {"service": "sonarr", "topic": "queue"})
    assert not out["ok"] and "offline" in out["error"]
    assert not tk.call("describe_api", {"service": "plex", "topic": "x"})["ok"]


def test_scrub_is_recursive_and_keeps_the_rest():
    value = {
        "a": [{"ApiKey": "k", "Passkey": "p", "n": 1}],
        "url": "https://x/announce?passkey=1",
        "b": "fine",
    }
    out = passthrough.scrub(value)
    assert out == {
        "a": [{"ApiKey": "[redacted]", "Passkey": "[redacted]", "n": 1}],
        "url": "[redacted tracker url]",
        "b": "fine",
    }
    assert time.time() > 0  # keeps the import honest
