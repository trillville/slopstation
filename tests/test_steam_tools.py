"""Test the Steam data and client tools, and the TV and PC state tools."""

import json
import types

import pytest

from helpers import CapturingLog
from slopstation import config, gamepc, sessionlock, statefile
from slopstation.agent.llm import assistant
from slopstation.agent.tools import library, steamstore

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


@pytest.fixture
def log():
    return CapturingLog("voice")


class FakeSteam:
    def __init__(self, enrolled=True):
        self.enrolled = enrolled
        self.calls = []
        self.apps = {INSTALLED: {"paused": True, "changing": True}}

    def available(self):
        return self.enrolled

    def access_token(self):
        return "tok"

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

    def _post(self, method, data):
        self.calls.append(("post", method, dict(data)))
        return None, "1"


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
    monkeypatch.setattr(steamstore, "fetch_appdetails", lambda a: {"dlc": [1, 2]})
    monkeypatch.setattr(
        steamstore,
        "fetch_dlc",
        lambda a, d=None: [
            {"appid": 1, "name": "Pack", "final": "$4.99", "discount": 0}
        ],
    )
    monkeypatch.setattr(
        steamstore, "fetch_requirements", lambda a, d=None: {"minimum": "8 GB RAM"}
    )
    monkeypatch.setattr(
        steamstore,
        "fetch_release",
        lambda a, d=None: {
            "date": "2 Feb, 2021",
            "coming_soon": False,
            "developers": ["Iron Gate"],
            "publishers": [],
        },
    )
    monkeypatch.setattr(steamstore, "fetch_players_now", lambda a: 12345)
    monkeypatch.setattr(
        steamstore,
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
        steamstore,
        "fetch_players_now",
        lambda a: (_ for _ in ()).throw(RuntimeError("down")),
    )
    out = tk.call("get_game_details", {"appid": INSTALLED, "facets": ["players_now"]})
    assert out["ok"] and "players_now" not in out


def test_list_games_new_sources_and_the_moved_one(rig, monkeypatch):
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
        steamstore,
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
    moved = tk.call("list_games", {"source": "downloading"})
    assert not moved["ok"] and "download_status" in moved["error"]
    assert not tk.call("list_games", {"source": "nope"})["ok"]


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
        steamstore,
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
    monkeypatch.setattr(steamstore, "fetch_friends", lambda: None)
    assert "steamApiKey" in tk.call("friends", {})["error"]


def test_achievements_new_releases_and_wishlist_edit(rig, monkeypatch):
    tk, dispatch, steam, _ = rig
    monkeypatch.setattr(
        steamstore,
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
    monkeypatch.setattr(steamstore, "fetch_achievements", lambda a: None)
    assert not tk.call("my_achievements", {"appid": INSTALLED})["ok"]
    monkeypatch.setattr(
        steamstore,
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
    assert out["ok"] and steam.calls[-1][1] == "IWishlistService/AddToWishlist/v1"
    assert steam.calls[-1][2] == {"access_token": "tok", "appid": UNOWNED}
    steam._post = lambda m, d: (None, "15")
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
    assert out["ok"] and "does not yet show" in out["detail"]
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


def test_steam_client_tools_need_the_account_session(catalog, log):
    dispatch = types.SimpleNamespace(dry_run=False, utterance=None)
    tk = assistant.Toolkit(dispatch, log)
    assert "download_status" not in tk.offered and "uninstall_game" not in tk.offered
    unenrolled = assistant.Toolkit(dispatch, log, steam=FakeSteam(enrolled=False))
    unenrolled.load(["download_status"])
    assert "enrolled" in unenrolled.call("download_status", {})["error"]


def test_tv_status_pc_status_and_pc_power(rig, monkeypatch):
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
    # Power: wake sends the packet; sleep is refused while a session is live.
    woke = []
    monkeypatch.setattr("slopstation.couch.wol", lambda: woke.append(1))
    assert tk.call("pc_power", {"action": "wake"})["ok"] and woke == [1]
    monkeypatch.setattr(sessionlock, "active", lambda *a: True)
    assert "end it first" in tk.call("pc_power", {"action": "sleep"})["error"]
    monkeypatch.setattr(sessionlock, "active", lambda *a: False)
    sent = []
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: sent.append(cmd) or "OK")
    assert tk.call("pc_power", {"action": "sleep"})["ok"] and sent == [
        "sleep --turn aa0001"
    ]
    monkeypatch.setattr(gamepc, "ssh", lambda cmd, **kw: "BUSY:12345")
    assert "refused" in tk.call("pc_power", {"action": "sleep"})["error"]
    assert not tk.call("pc_power", {"action": "reboot"})["ok"]
    dispatch.dry_run = True
    assert tk.call("pc_power", {"action": "sleep"})["dry_run"]


# --- store parsing for the new helpers ------------------------------------------


def test_store_helpers_parse_steams_shapes(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=20):
        calls.append(url)
        if "appdetails" in url:
            return {
                "1": {
                    "success": True,
                    "data": {
                        "dlc": [2],
                        "pc_requirements": {
                            "minimum": "<strong>Memory:</strong> 8 GB<br>"
                        },
                        "release_date": {"date": "1 Jan, 2024", "coming_soon": False},
                        "developers": ["Dev"],
                    },
                }
            }
        if "GetItems" in url:
            return {
                "response": {
                    "store_items": [
                        {
                            "appid": 2,
                            "name": "DLC Two",
                            "best_purchase_option": {
                                "formatted_final_price": "$4.99",
                                "discount_pct": 0,
                                "final_price_in_cents": 499,
                            },
                        }
                    ]
                }
            }
        if "GetNumberOfCurrentPlayers" in url:
            return {"response": {"player_count": 777}}
        if "GetSchemaForGame" in url:
            return {
                "game": {
                    "availableGameStats": {
                        "achievements": [
                            {
                                "name": "a",
                                "displayName": "First",
                                "description": "Do it",
                            },
                            {"name": "b", "displayName": "Second", "description": ""},
                        ]
                    }
                }
            }
        if "GetPlayerAchievements" in url:
            return {
                "playerstats": {
                    "achievements": [
                        {"apiname": "a", "achieved": 1, "unlocktime": 1756000000},
                        {"apiname": "b", "achieved": 0},
                    ]
                }
            }
        if "GetGlobalAchievementPercentagesForApp" in url:
            return {
                "achievementpercentages": {
                    "achievements": [
                        {"name": "a", "percent": 80.5},
                        {"name": "b", "percent": 12.0},
                    ]
                }
            }
        if "GetFriendList" in url:
            return {"friendslist": {"friends": [{"steamid": "1"}, {"steamid": "2"}]}}
        if "GetPlayerSummaries" in url:
            return {
                "response": {
                    "players": [
                        {
                            "personaname": "Zed",
                            "personastate": 0,
                            "lastlogoff": 1756000000,
                        },
                        {
                            "personaname": "Amy",
                            "personastate": 1,
                            "gameid": "1145360",
                            "gameextrainfo": "Hades",
                        },
                    ]
                }
            }
        if "featuredcategories" in url:
            return {
                "top_sellers": {
                    "items": [
                        {
                            "id": 9,
                            "name": "Seller",
                            "discount_percent": 10,
                            "final_price": 899,
                        },
                        {"id": 228980, "name": "Redist"},
                    ]
                }
            }
        if "GetWishlist" in url:
            return {
                "response": {
                    "items": [{"appid": 2, "priority": 2}, {"appid": 3, "priority": 1}]
                }
            }
        raise AssertionError(url)

    monkeypatch.setattr(steamstore, "_get", fake_get)
    monkeypatch.setattr(library, "steam_creds", lambda: ("K", "7656119"))
    data = steamstore.fetch_appdetails(1)
    assert steamstore.fetch_dlc(1, data) == [
        {"appid": 2, "name": "DLC Two", "final": "$4.99", "discount": 0, "price": 499}
    ]
    assert steamstore.fetch_requirements(1, data) == {"minimum": "Memory: 8 GB"}
    assert steamstore.fetch_release(1, data)["developers"] == ["Dev"]
    assert steamstore.fetch_players_now(1) == 777
    ach = steamstore.fetch_achievements(1)
    assert ach["total"] == 2 and ach["unlocked"] == 1 and ach["percent"] == 50
    assert (
        ach["recent"][0]["name"] == "First" and ach["recent"][0]["global_pct"] == 80.5
    )
    assert (
        ach["next_up"][0]["name"] == "Second" and ach["next_up"][0]["unlocked"] is None
    )
    friends = steamstore.fetch_friends()
    assert [f["name"] for f in friends] == ["Amy", "Zed"] and friends[0][
        "playing"
    ] == "Hades"
    assert friends[1]["state"] == "offline" and friends[1]["last_seen"]
    top = steamstore.fetch_featured("top_sellers")
    assert top == [{"appid": 9, "name": "Seller", "discount": 10, "final": 8.99}]
    assert steamstore.fetch_featured("bogus") == []
    wl = steamstore.fetch_wishlist("7656119")
    assert [w["appid"] for w in wl] == [3, 2] and wl[1]["name"] == "DLC Two"
    monkeypatch.setattr(library, "steam_creds", lambda: None)
    assert (
        steamstore.fetch_achievements(1) is None and steamstore.fetch_friends() is None
    )
