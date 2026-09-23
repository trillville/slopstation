"""Tests for Steam store parsing and the completion-time cache."""

import dataclasses
import json
import sys

import pytest

from helpers import CapturingLog
from slopstation import config, statefile
from slopstation.agent.steam import library, store

# appid -> (name, discount_pct, final_cents, formatted). The fake GetItems
# echoes back whatever appids the caller asked for.
ITEMS = {
    10: ("Wish One", 50, 500, "$5.00"),
    11: ("Wish Two", 0, 2000, "$20.00"),
    12: ("Wish Three", 75, 250, "$2.50"),
    100: ("Trend A", 0, 5999, "$59.99"),
    101: ("Trend B", 0, 0, "Free"),
    200: ("Search A", 0, 1500, "$15.00"),
    201: ("Search B", 10, 2500, "$25.00"),
}

KEYED = {"steamApiKey": "X" * 40, "steamId64": "7656119"}


@dataclasses.dataclass
class FakeStore:
    """store._get answering canned JSON. Every /search/results query is
    kept for the tag assertions."""

    search_params: list = dataclasses.field(default_factory=list)

    def __call__(self, url, params=None, timeout=20):
        p = params or {}
        if "featuredcategories" in url:
            return {
                "specials": {
                    "items": [
                        {
                            "id": 1,
                            "name": "Special A",
                            "discount_percent": 25,
                            "final_price": 1499,
                        },
                        {
                            "id": 228980,
                            "name": "Redistributables",
                            "discount_percent": 90,
                        },  # NOT_GAMES
                        {
                            "id": 2,
                            "name": "Special B",
                            "discount_percent": 0,
                            "final_price": 999,
                        },
                    ]
                }
            }
        if "GetWishlist" in url:
            return {
                "response": {"items": [{"appid": 10}, {"appid": 11}, {"appid": 12}]}
            }
        if "GetItems" in url:
            ids = [x["appid"] for x in json.loads(p["input_json"])["ids"]]
            return {
                "response": {
                    "store_items": [
                        {
                            "appid": a,
                            "name": ITEMS[a][0],
                            "best_purchase_option": {
                                "discount_pct": ITEMS[a][1],
                                "final_price_in_cents": ITEMS[a][2],
                                "formatted_final_price": ITEMS[a][3],
                            },
                        }
                        for a in ids
                        if a in ITEMS
                    ]
                }
            }
        if "GetMostPlayedGames" in url:
            return {"response": {"ranks": [{"appid": 100}, {"appid": 101}]}}
        if "search/results" in url:
            self.search_params.append(p)
            return {
                "results_html": '<a data-ds-appid="200"></a>'
                '<a data-ds-appid="201"></a>'
                '<a data-ds-appid="200"></a>'
            }  # dupe ignored
        if "appreviews" in url:
            return {
                "query_summary": {
                    "review_score_desc": "Very Positive",
                    "total_positive": 90,
                    "total_reviews": 100,
                },
                "reviews": [{"review": "great"}, {"review": "good"}, {"review": ""}],
            }
        if "GetNewsForApp" in url:
            if p.get("tags") == "patchnotes":
                return {
                    "appnews": {"newsitems": [{"title": "Patch 1", "date": 1700000000}]}
                }
            return {
                "appnews": {"newsitems": [{"title": "Any News", "date": 1700000000}]}
            }
        if "GetTagList" in url:
            return {
                "response": {
                    "tags": [
                        {"name": "Roguelike", "tagid": 1716},
                        {"name": "Co-op", "tagid": 3843},
                    ]
                }
            }
        if "GetRecentlyPlayedGames" in url:
            return {
                "response": {
                    "games": [{"appid": 55, "name": "Recent X", "playtime_2weeks": 300}]
                }
            }
        return None


@pytest.fixture
def fake_store(monkeypatch):
    """Mock store requests and remove Steam credentials."""
    fake = FakeStore()
    monkeypatch.setattr(store, "_get", fake)
    monkeypatch.setattr(config, "secrets", lambda: {})
    return fake


@pytest.fixture
def keyed(fake_store, monkeypatch):
    """The same store with a Steam key and id on file."""
    monkeypatch.setattr(config, "secrets", lambda: dict(KEYED))
    return fake_store


def test_specials_are_parsed_and_filtered(fake_store):
    # NOT_GAMES filtered, cents -> dollars
    sp = store.fetch_featured("specials")
    assert [s["appid"] for s in sp] == [1, 2], sp
    assert sp[0] == {"appid": 1, "name": "Special A", "discount": 25, "final": 14.99}, (
        sp[0]
    )


def test_store_items_price_in_batches(fake_store):
    # name/price/discount, missing appids simply absent
    items = store.store_items([10, 11, 999])
    assert set(items) == {10, 11}, items
    assert items[10] == {
        "name": "Wish One",
        "final": "$5.00",
        "discount": 50,
        "price": 500,
    }, items[10]
    # ...and chunks past the 100-per-batch cap: id 12 sits at position 120, so
    # it only prices if a second batch was fetched.
    big = [10] + list(range(900000, 900119)) + [12]
    assert set(store.store_items(big)) == {10, 12}, "the >100 tail was dropped"


def test_trending_ranks_with_names(fake_store):
    # rank + name via GetItems
    tr = store.fetch_trending()
    assert tr[0] == {"appid": 100, "rank": 1, "name": "Trend A"}, tr[0]
    assert tr[1]["rank"] == 2


def test_store_search_prices_deduped_capsules(fake_store):
    # appids from capsule attrs (deduped) -> priced
    rows = store.fetch_store_search(term="anything")
    assert [r["appid"] for r in rows] == [200, 201], rows
    # Filter by the GetItems price, not the search-page price.
    clipped = store.fetch_store_search(term="anything", max_price=20)
    assert [r["appid"] for r in clipped] == [200], clipped  # 201 is $25 -> out


def test_reviews_summary_and_snippets(fake_store):
    rv = store.fetch_reviews(1)
    assert (
        rv["desc"] == "Very Positive"
        and rv["positive_pct"] == 90
        and rv["total"] == 100
    ), rv
    assert rv["snippets"] == ["great", "good"], rv  # the "" one dropped


def test_news_prefers_patchnotes(fake_store):
    # patchnotes preferred, fallback to any
    assert store.fetch_news(1)[0]["title"] == "Patch 1"


def test_tag_map_is_keyed_and_cached(fake_store, monkeypatch):
    # A missing key disables the tag map.
    assert store._tag_map() == {}
    monkeypatch.setattr(config, "secrets", lambda: dict(KEYED))
    tmap = store._tag_map()
    assert tmap.get("roguelike") == 1716 and tmap.get("co-op") == 3843, tmap
    assert store.tagmap_file().exists()  # cached to disk


def test_search_tags_match_loosely(keyed):
    # Tag matching ignores punctuation and case both ways ("Rogue-like"/"Co op"
    # vs Steam's "Roguelike"/"Co-op"); an exact lookup would drop the tag and
    # silently widen the search.
    store.fetch_store_search(term="x", tags=["Rogue-like", "CO OP"])
    assert keyed.search_params[-1].get("tags") == "1716,3843", keyed.search_params[-1]
    store.fetch_store_search(term="x", tags=["Not A Real Tag"])
    assert "tags" not in keyed.search_params[-1], "unknown tags must drop, not 404"


def test_hltb_fails_soft_then_hits_the_cache(fake_store, monkeypatch):
    # A None sys.modules entry makes the import raise, so this stays offline
    # even where howlongtobeatpy is installed.
    monkeypatch.setitem(sys.modules, "howlongtobeatpy", None)
    assert store.fetch_hltb("Some Game With No Lib") is None
    statefile.write(store.hltb_cache_file(), {library.fuzzy_key("Hades"): {"main": 21}})
    assert store.fetch_hltb("hades") == {"main": 21}  # cache hit, no import


def test_refresh_deals_writes_the_file_list_games_reads(keyed, monkeypatch):
    log = CapturingLog("library")
    monkeypatch.setattr(store, "log", log)
    store.refresh_deals()
    deals = store.load_deals()
    assert "deals_synced" in log.events(), log.events()
    assert deals["specials"][0]["appid"] == 1
    # 75% then 50%; 11 (0%) dropped
    assert [g["appid"] for g in deals["wishlist_on_sale"]] == [12, 10], deals
    assert "refreshed" in deals


def test_recently_played_needs_a_key(keyed):
    # parsed 2-week hours
    rec = store.fetch_recently_played()
    assert rec == [{"appid": 55, "name": "Recent X", "hours2w": 5.0}], rec


# --- the detail helpers: DLC, requirements, players, achievements, friends --


def test_store_helpers_parse_steams_shapes(monkeypatch):
    def fake_get(url, params=None, timeout=20):
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
        if "GetWishlist" in url:
            return {
                "response": {
                    "items": [{"appid": 2, "priority": 2}, {"appid": 3, "priority": 1}]
                }
            }
        raise AssertionError(url)

    monkeypatch.setattr(store, "_get", fake_get)
    monkeypatch.setattr(library, "steam_creds", lambda: ("K", "7656119"))
    data = store.fetch_appdetails(1)
    assert store.fetch_dlc(data) == [
        {"appid": 2, "name": "DLC Two", "final": "$4.99", "discount": 0, "price": 499}
    ]
    assert store.fetch_requirements(data) == {"minimum": "Memory: 8 GB"}
    assert store.fetch_release(data)["developers"] == ["Dev"]
    assert store.fetch_players_now(1) == 777
    ach = store.fetch_achievements(1)
    assert ach["total"] == 2 and ach["unlocked"] == 1 and ach["percent"] == 50
    assert (
        ach["recent"][0]["name"] == "First" and ach["recent"][0]["global_pct"] == 80.5
    )
    assert (
        ach["next_up"][0]["name"] == "Second" and ach["next_up"][0]["unlocked"] is None
    )
    friends = store.fetch_friends()
    assert [f["name"] for f in friends] == ["Amy", "Zed"] and friends[0][
        "playing"
    ] == "Hades"
    assert friends[1]["state"] == "offline" and friends[1]["last_seen"]
    wl = store.fetch_wishlist("7656119")
    assert [w["appid"] for w in wl] == [3, 2] and wl[1]["name"] == "DLC Two"
    monkeypatch.setattr(library, "steam_creds", lambda: None)
    assert store.fetch_achievements(1) is None and store.fetch_friends() is None
