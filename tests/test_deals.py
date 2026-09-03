"""Library layer-4 store-question parsers. Canned JSON through the
single _get seam (no network), pinning the parse shapes a keyless checkout
can't otherwise see, plus the hltb cache.
"""

import dataclasses
import json
import sys

import pytest

from helpers import CapturingLog
from slopstation import config
from slopstation.agent.tools import library, steamstore

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
    """steamstore._get answering canned JSON. Every /search/results query is
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
def store(monkeypatch):
    """The HTTP seam swapped and no key. The deals, facet and tag-map files
    already resolve under this test's home. The secrets are pinned BEFORE
    anything runs: fetch_store_search -> _tag_map reaches for a key, and a
    real secrets.json takes the keyed path."""
    fake = FakeStore()
    monkeypatch.setattr(steamstore, "_get", fake)
    monkeypatch.setattr(config, "secrets", lambda: {})
    return fake


@pytest.fixture
def keyed(store, monkeypatch):
    """The same store with a Steam key and id on file."""
    monkeypatch.setattr(config, "secrets", lambda: dict(KEYED))
    return store


def test_specials_are_parsed_and_filtered(store):
    # NOT_GAMES filtered, cents -> dollars
    sp = steamstore.fetch_specials()
    assert [s["appid"] for s in sp] == [1, 2], sp
    assert sp[0] == {"appid": 1, "name": "Special A", "discount": 25, "final": 14.99}, (
        sp[0]
    )


def test_store_items_price_in_batches(store):
    # name/price/discount, missing appids simply absent
    items = steamstore.store_items([10, 11, 999])
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
    assert set(steamstore.store_items(big)) == {10, 12}, "the >100 tail was dropped"


def test_wishlist_on_sale_is_discounted_best_first(store):
    ws = steamstore.fetch_wishlist_on_sale("7656119")
    assert [g["appid"] for g in ws] == [12, 10], ws  # 75% then 50%; 11 (0%) dropped
    assert ws[0]["discount"] == 75


def test_trending_ranks_with_names(store):
    # rank + name via GetItems
    tr = steamstore.fetch_trending()
    assert tr[0] == {"appid": 100, "rank": 1, "name": "Trend A"}, tr[0]
    assert tr[1]["rank"] == 2


def test_store_search_prices_deduped_capsules(store):
    # appids from capsule attrs (deduped) -> priced
    rows = steamstore.fetch_store_search(term="anything")
    assert [r["appid"] for r in rows] == [200, 201], rows
    # max_price clips on the AUTHORITATIVE GetItems price, not the search page
    clipped = steamstore.fetch_store_search(term="anything", max_price=20)
    assert [r["appid"] for r in clipped] == [200], clipped  # 201 is $25 -> out


def test_reviews_summary_and_snippets(store):
    rv = steamstore.fetch_reviews(1)
    assert (
        rv["desc"] == "Very Positive"
        and rv["positive_pct"] == 90
        and rv["total"] == 100
    ), rv
    assert rv["snippets"] == ["great", "good"], rv  # the "" one dropped


def test_news_prefers_patchnotes(store):
    # patchnotes preferred, fallback to any
    assert steamstore.fetch_news(1)[0]["title"] == "Patch 1"


def test_tag_map_is_keyed_and_cached(store, monkeypatch):
    # keyed only; fail-soft to {} without a key
    assert steamstore._tag_map() == {}
    monkeypatch.setattr(config, "secrets", lambda: dict(KEYED))
    tmap = steamstore._tag_map()
    assert tmap.get("roguelike") == 1716 and tmap.get("co-op") == 3843, tmap
    assert steamstore.tagmap_file().exists()  # cached to disk


def test_search_tags_match_loosely(keyed):
    # Tag matching ignores punctuation and case both ways ("Rogue-like"/"Co op"
    # vs Steam's "Roguelike"/"Co-op"); an exact lookup dropped the tag and
    # silently widened the search (2026-08-14).
    steamstore.fetch_store_search(term="x", tags=["Rogue-like", "CO OP"])
    assert keyed.search_params[-1].get("tags") == "1716,3843", keyed.search_params[-1]
    steamstore.fetch_store_search(term="x", tags=["Not A Real Tag"])
    assert "tags" not in keyed.search_params[-1], "unknown tags must drop, not 404"


def test_hltb_fails_soft_then_hits_the_cache(store, monkeypatch):
    # A None sys.modules entry makes the import raise, so this stays offline
    # even where howlongtobeatpy is installed.
    monkeypatch.setitem(sys.modules, "howlongtobeatpy", None)
    assert steamstore.fetch_hltb("Some Game With No Lib") is None
    steamstore._save_facets({library.fuzzy_key("Hades"): {"hltb": {"main": 21}}})
    assert steamstore.fetch_hltb("hades") == {"main": 21}  # cache hit, no import


def test_refresh_deals_writes_the_file_list_games_reads(keyed, monkeypatch):
    log = CapturingLog("library")
    monkeypatch.setattr(steamstore, "log", log)
    steamstore.refresh_deals()
    deals = steamstore.load_deals()
    assert "deals_synced" in log.events(), log.events()
    assert deals["specials"][0]["appid"] == 1
    assert [g["appid"] for g in deals["wishlist_on_sale"]] == [12, 10], deals
    assert "refreshed" in deals


def test_recently_played_needs_a_key(keyed):
    # parsed 2-week hours
    rec = steamstore.fetch_recently_played()
    assert rec == [{"appid": 55, "name": "Recent X", "hours2w": 5.0}], rec
