"""Test the Steam data and client tools, and the TV and PC state tools."""

import json
import types

import pytest

from slopstation import config, gamepc, sessionlock, statefile
from slopstation.agent.llm import assistant
from slopstation.agent.steam import library, store

INSTALLED, OWNED, UNOWNED = 892970, 413150, 1478500
INDEX = {
    "installed": [
        {
            "appid": INSTALLED,
            "name": "Valheim",
            "state": 4,
            "size": 1,
            "lastPlayed": 0,
            "updated": 1756000000,
        }
    ],
    "owned": {
        str(INSTALLED): {
            "hours": 12.0,
            "hours2w": 3.0,
            "last": 1756000000,
            "name": "Valheim",
        },
        str(OWNED): {"hours": 0, "hours2w": 0, "last": 0, "name": "Stardew Valley"},
        "228980": {
            "hours": 0,
            "hours2w": 0,
            "last": 0,
            "name": "Steamworks Common Redistributables",
        },
    },
}
META = {
    str(INSTALLED): {
        "tags": ["Survival", "Co-op"],
        "genres": ["Action"],
        "controller": "full",
    },
    str(OWNED): {
        "tags": ["Farming Sim", "Relaxing"],
        "genres": ["Indie"],
        "controller": "full",
    },
}


@pytest.fixture
def catalog():
    statefile.write(library.library_file(), INDEX)
    statefile.write(library.meta_cache_file(), META)


class FakeSteam:
    def __init__(self):
        self.calls = []

    def client_online(self):
        return True

    def download_status(self):
        return [
            {
                "appid": INSTALLED,
                "name": "Valheim",
                "percent": 40,
                "paused": False,
                "queue": 0,
                "phase": "downloading",
            }
        ]

    def set_update_state(self, appid, action):
        self.calls.append(("state", appid, action))
        paused = action == "pause"
        return {
            "ok": True,
            "action": action,
            "paused": paused,
            "changing": True,
            "verified": True,
        }

    def enable_downloads(self, enable):
        self.calls.append(("switch", enable))
        return {"ok": True, "downloads_enabled": enable}

    def uninstall(self, appid):
        self.calls.append(("uninstall", appid))
        return {"ok": True, "detail": "Steam is uninstalling it", "verified": True}

    def wishlist(self, appid, add):
        self.calls.append(("wishlist", appid, add))
        return {"ok": True, "appid": appid, "action": "add" if add else "remove"}


@pytest.fixture
def rig(catalog, log, monkeypatch):
    playing = {"appid": 0}
    dispatch = types.SimpleNamespace(
        dry_run=False,
        utterance=types.SimpleNamespace(turn="aa0001", asked=""),
        now_playing=lambda: types.SimpleNamespace(
            ok=True, detail=str(playing["appid"])
        ),
        tv=types.SimpleNamespace(
            power_state=lambda: "on", volume=lambda: 14, muted=lambda: False
        ),
    )
    steam = FakeSteam()
    tk = assistant.Toolkit(dispatch, log, steam=steam)
    tk.load(assistant.REGISTRY.names())
    monkeypatch.setattr(
        config, "secrets", lambda: {"steamApiKey": "K" * 32, "steamId64": "7656119"}
    )
    return tk, dispatch, steam, playing


def test_game_details_gathers_the_new_facets(rig, monkeypatch):
    tk, _, _, _ = rig
    monkeypatch.setattr(store, "fetch_appdetails", lambda a: {"dlc": [1, 2]})
    monkeypatch.setattr(
        store,
        "fetch_dlc",
        lambda d: [{"appid": 1, "name": "Pack", "final": "$4.99", "discount": 0}],
    )
    monkeypatch.setattr(store, "fetch_requirements", lambda d: {"minimum": "8 GB RAM"})
    monkeypatch.setattr(
        store,
        "fetch_release",
        lambda d: {
            "date": "2 Feb, 2021",
            "coming_soon": False,
            "developers": ["Iron Gate"],
            "publishers": [],
        },
    )
    monkeypatch.setattr(store, "fetch_players_now", lambda a: 12345)
    monkeypatch.setattr(
        store,
        "fetch_achievements",
        lambda a: {
            "total": 10,
            "unlocked": 4,
            "percent": 40,
            "recent": [],
            "next_up": [],
            "rarest_held": [],
        },
    )
    out = tk.call(
        "get_game_details",
        {
            "appid": INSTALLED,
            "facets": ["dlc", "requirements", "release", "players_now", "achievements"],
        },
    )
    assert out["ok"] and out["name"] == "Valheim" and out["installed"]
    assert out["dlc"][0]["name"] == "Pack" and out["requirements"] == {
        "minimum": "8 GB RAM"
    }
    assert out["release"]["developers"] == ["Iron Gate"] and out["players_now"] == 12345
    assert out["achievements"]["percent"] == 40 and out["tags"] == ["Survival", "Co-op"]
    # A facet that fails is logged and left out, not a broken turn.
    monkeypatch.setattr(
        store,
        "fetch_players_now",
        lambda a: (_ for _ in ()).throw(RuntimeError("down")),
    )
    out = tk.call("get_game_details", {"appid": INSTALLED, "facets": ["players_now"]})
    assert out["ok"] and "players_now" not in out


def test_list_games_reads_the_owned_sources_and_the_wishlist(rig, monkeypatch):
    tk, _, _, _ = rig
    unplayed = tk.call("list_games", {"source": "unplayed"})
    assert unplayed["ok"] and [g["name"] for g in unplayed["games"]] == [
        "Stardew Valley"
    ]
    most = tk.call("list_games", {"source": "most_played"})
    assert most["games"][0]["name"] == "Valheim" and most["games"][0]["hours"] == 12.0
    updated = tk.call("list_games", {"source": "recently_updated"})
    assert updated["count"] == 1 and updated["games"][0]["updated"]
    monkeypatch.setattr(
        store,
        "fetch_wishlist",
        lambda sid: [
            {
                "appid": UNOWNED,
                "priority": 1,
                "name": "Wanted",
                "final": "$9.99",
                "discount": 0,
                "price": 999,
            }
        ],
    )
    wl = tk.call("list_games", {"source": "wishlist"})
    assert wl["ok"] and wl["games"][0]["name"] == "Wanted"


def test_search_library_playtime_and_friends(rig, monkeypatch):
    tk, _, _, _ = rig
    coop = tk.call("search_library", {"term": "co-op"})
    assert (
        coop["count"] == 1
        and coop["games"][0]["name"] == "Valheim"
        and coop["games"][0]["tags"]
    )
    never = tk.call("search_library", {"played": False})
    assert [g["name"] for g in never["games"]] == ["Stardew Valley"]
    assert (
        tk.call("search_library", {"installed": False, "controller": "full"})["count"]
        == 1
    )
    assert tk.call("search_library", {"min_hours": 5})["count"] == 1
    assert (
        tk.call("search_library", {"sort": "name"})["games"][0]["name"]
        == "Stardew Valley"
    )
    assert not tk.call("search_library", {"sort": "colour"})["ok"]
    # Redistributables never appear.
    assert all(
        "Redistributables" not in g["name"]
        for g in tk.call("search_library", {})["games"]
    )
    pt = tk.call("playtime", {"period": "two_weeks"})
    assert pt["total_hours"] == 3.0 and pt["games"][0]["hours"] == 3.0
    assert tk.call("playtime", {})["total_hours"] == 12.0
    assert not tk.call("playtime", {"period": "decade"})["ok"]
    monkeypatch.setattr(
        store,
        "fetch_friends",
        lambda: [
            {
                "name": "Bo",
                "state": "playing",
                "playing": "Hades",
                "appid": 1145360,
                "last_seen": None,
            },
            {
                "name": "Al",
                "state": "offline",
                "playing": None,
                "appid": None,
                "last_seen": "2026-09-01",
            },
        ],
    )
    fr = tk.call("friends", {})
    assert fr["count"] == 2 and fr["online"] == 1 and fr["friends"][0]["name"] == "Bo"
    monkeypatch.setattr(store, "fetch_friends", lambda: None)
    assert "steamApiKey" in tk.call("friends", {})["error"]


def test_achievements_new_releases_and_wishlist_edit(rig, monkeypatch):
    tk, dispatch, steam, _ = rig
    monkeypatch.setattr(
        store,
        "fetch_achievements",
        lambda a: {
            "total": 10,
            "unlocked": 4,
            "percent": 40,
            "recent": [],
            "next_up": [],
            "rarest_held": [],
        },
    )
    ach = tk.call("my_achievements", {"appid": INSTALLED})
    assert ach["ok"] and ach["name"] == "Valheim" and ach["unlocked"] == 4
    assert not tk.call("my_achievements", {"appid": UNOWNED})["ok"], "owned games only"
    monkeypatch.setattr(store, "fetch_achievements", lambda a: None)
    assert not tk.call("my_achievements", {"appid": INSTALLED})["ok"]
    monkeypatch.setattr(
        store,
        "fetch_featured",
        lambda s: [{"appid": 5, "name": "Fresh", "discount": 0, "final": 19.99}],
    )
    nr = tk.call("new_releases", {"section": "top_sellers", "limit": 1})
    assert (
        nr["ok"]
        and nr["section"] == "top_sellers"
        and nr["games"][0]["name"] == "Fresh"
    )
    assert not tk.call("new_releases", {"section": "hidden_gems"})["ok"]
    out = tk.call("wishlist_edit", {"appid": UNOWNED, "action": "add"})
    assert out["ok"] and steam.calls[-1] == ("wishlist", UNOWNED, True)
    steam.wishlist = lambda a, add: {"ok": False, "error": "Steam refused (code 15)"}
    assert (
        "code 15"
        in tk.call("wishlist_edit", {"appid": UNOWNED, "action": "remove"})["error"]
    )
    assert not tk.call("wishlist_edit", {"appid": 0, "action": "add"})["ok"]
    assert not tk.call("wishlist_edit", {"appid": 1, "action": "burn"})["ok"]
    dispatch.dry_run = True
    assert tk.call("wishlist_edit", {"appid": UNOWNED, "action": "add"})["dry_run"]


def test_steam_client_tools_report_what_steam_holds(rig, log):
    tk, dispatch, steam, playing = rig
    ds = tk.call("download_status", {})
    assert (
        ds["ok"] and ds["count"] == 1 and ds["downloads"][0]["phase"] == "downloading"
    )
    assert tk.call("pause_downloads", {"appid": INSTALLED})["paused"] is True
    assert steam.calls[-1] == ("state", INSTALLED, "pause")
    assert tk.call("resume_downloads", {})["downloads_enabled"] is True
    assert steam.calls[-1] == ("switch", True)
    assert tk.call("pause_downloads", {})["downloads_enabled"] is False
    assert not tk.call("pause_downloads", {"appid": "x"})["ok"]
    # Unverified: Steam accepted, the app list disagrees - said plainly.
    steam.set_update_state = lambda a, act: {
        "ok": True,
        "action": act,
        "paused": False,
        "verified": False,
    }
    out = tk.call("pause_downloads", {"appid": INSTALLED})
    assert out["ok"] and "still shows the download as running" in out["detail"]
    # Uninstall: not installed, running, gated, then done.
    assert not tk.call("uninstall_game", {"appid": OWNED})["ok"]
    playing["appid"] = INSTALLED
    assert "quit it first" in tk.call("uninstall_game", {"appid": INSTALLED})["error"]
    playing["appid"] = 0
    asked = tk.call("uninstall_game", {"appid": INSTALLED})
    assert (
        not asked["ok"] and asked["acknowledgment"] == "Uninstall Valheim from the PC?"
    )
    assert not any(c[0] == "uninstall" for c in steam.calls)
    dispatch.utterance = types.SimpleNamespace(turn="aa0002", asked="yes")
    done = tk.call("uninstall_game", {"appid": INSTALLED})
    assert (
        done["ok"]
        and done["name"] == "Valheim"
        and steam.calls[-1] == ("uninstall", INSTALLED)
    )


def test_tv_status_pc_status_wake_and_sleep(rig, monkeypatch):
    tk, dispatch, steam, _ = rig
    tv = tk.call("tv_status", {})
    assert tv == {"ok": True, "power": "on", "volume": 14, "muted": False}
    dispatch.tv.volume = lambda: (_ for _ in ()).throw(TimeoutError("no answer"))
    tv = tk.call("tv_status", {})
    assert tv["ok"] and tv["volume"] is None and "volume" in tv["errors"][0]
    answers = {
        "status": "9f2c1a",
        "playing": str(INSTALLED),
        "disk": json.dumps(
            [
                {
                    "root": "c:\\steam",
                    "drive": "C:\\",
                    "free": 100 * 1024**3,
                    "total": 1000 * 1024**3,
                }
            ]
        ),
    }
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: answers[cmd.split()[0]])
    pc = tk.call("pc_status", {})
    assert pc["reachable"] and pc["ready"] and pc["session_turn"] == "9f2c1a"
    assert pc["running"] == {"appid": INSTALLED, "name": "Valheim"}
    assert pc["steam_drives"] == [
        {"drive": "C:\\", "free_gb": 100.0, "total_gb": 1000.0}
    ]
    assert pc["steam_online"] is True

    def down(cmd, **kw):
        raise TimeoutError("ssh timed out")

    monkeypatch.setattr(gamepc, "ssh", down)
    pc = tk.call("pc_status", {})
    assert pc["ok"] and pc["reachable"] is False and "asleep" in pc["detail"]
    # ssh exit 255 is no connection; any other exit is the PC answering.
    import subprocess

    def denied(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(gamepc, "ssh", denied)
    pc = tk.call("pc_status", {})
    assert pc["reachable"] is True and "version skew" in pc["detail"]

    def unreachable(cmd, **kw):
        raise subprocess.CalledProcessError(255, cmd)

    monkeypatch.setattr(gamepc, "ssh", unreachable)
    assert tk.call("pc_status", {})["reachable"] is False
    # Two library roots on one drive report one drive.
    answers["disk"] = json.dumps(
        [
            {
                "root": "c:\\steam",
                "drive": "C:\\",
                "free": 1024**3,
                "total": 2 * 1024**3,
            },
            {
                "root": "c:\\games",
                "drive": "C:\\",
                "free": 1024**3,
                "total": 2 * 1024**3,
            },
        ]
    )
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: answers[cmd.split()[0]])
    assert len(tk.call("pc_status", {})["steam_drives"]) == 1
    # Power: wake sends the packet; sleep is refused while a session is live.
    woke = []
    monkeypatch.setattr("slopstation.couch.wol", lambda: woke.append(1))
    assert tk.call("wake_pc", {})["ok"] and woke == [1]
    monkeypatch.setattr(sessionlock, "active", lambda *a: True)
    assert "end it first" in tk.call("sleep_pc", {})["error"]
    monkeypatch.setattr(sessionlock, "active", lambda *a: False)
    sent = []
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: sent.append(cmd) or "OK")
    # Sleep asks first; the same call on a later turn's yes acts.
    asked = tk.call("sleep_pc", {})
    assert not asked["ok"] and asked["acknowledgment"] == "Put the PC to sleep?"
    assert sent == []
    dispatch.utterance = types.SimpleNamespace(turn="aa0002", asked="yes")
    assert tk.call("sleep_pc", {})["ok"] and sent == ["sleep --turn aa0002"]
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: "BUSY:12345")
    dispatch.utterance = types.SimpleNamespace(turn="aa0003", asked="sleep it")
    assert not tk.call("sleep_pc", {})["ok"]  # asked again
    dispatch.utterance = types.SimpleNamespace(turn="aa0004", asked="yes")
    assert "refused" in tk.call("sleep_pc", {})["error"]
    dispatch.dry_run = True
    assert tk.call("sleep_pc", {})["dry_run"] and tk.call("wake_pc", {})["dry_run"]
