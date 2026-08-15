"""Blind test: library layer-4 store-question parsers. Canned JSON is fed
through the single _get seam (no network), so this pins the parse shapes a
keyless checkout otherwise can't see - and the incremental hltb cache. Run:
    .venv\\Scripts\\python tests\\test_deals.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cglib
import library

# appid -> (name, discount_pct, final_cents, formatted). The fake GetItems
# echoes back whatever appids the caller asked for, from this fixture.
ITEMS = {10: ("Wish One", 50, 500, "$5.00"),
         11: ("Wish Two", 0, 2000, "$20.00"),
         12: ("Wish Three", 75, 250, "$2.50"),
         100: ("Trend A", 0, 5999, "$59.99"),
         101: ("Trend B", 0, 0, "Free"),
         200: ("Search A", 0, 1500, "$15.00"),
         201: ("Search B", 10, 2500, "$25.00")}


SEARCH_PARAMS = []          # every /search/results query, for the tag assertions


def fake_get(url, params=None, timeout=20):
    p = params or {}
    if "featuredcategories" in url:
        return {"specials": {"items": [
            {"id": 1, "name": "Special A", "discount_percent": 25, "final_price": 1499},
            {"id": 228980, "name": "Redistributables", "discount_percent": 90},  # NOT_GAMES
            {"id": 2, "name": "Special B", "discount_percent": 0, "final_price": 999}]}}
    if "GetWishlist" in url:
        return {"response": {"items": [{"appid": 10}, {"appid": 11}, {"appid": 12}]}}
    if "GetItems" in url:
        ids = [x["appid"] for x in json.loads(p["input_json"])["ids"]]
        return {"response": {"store_items": [
            {"appid": a, "name": ITEMS[a][0],
             "best_purchase_option": {"discount_pct": ITEMS[a][1],
                                      "final_price_in_cents": ITEMS[a][2],
                                      "formatted_final_price": ITEMS[a][3]}}
            for a in ids if a in ITEMS]}}
    if "GetMostPlayedGames" in url:
        return {"response": {"ranks": [{"appid": 100}, {"appid": 101}]}}
    if "search/results" in url:
        SEARCH_PARAMS.append(p)
        return {"results_html": '<a data-ds-appid="200"></a>'
                                '<a data-ds-appid="201"></a>'
                                '<a data-ds-appid="200"></a>'}  # dupe ignored
    if "appreviews" in url:
        return {"query_summary": {"review_score_desc": "Very Positive",
                                  "total_positive": 90, "total_reviews": 100},
                "reviews": [{"review": "great"}, {"review": "good"}, {"review": ""}]}
    if "GetNewsForApp" in url:
        if p.get("tags") == "patchnotes":
            return {"appnews": {"newsitems": [{"title": "Patch 1", "date": 1700000000}]}}
        return {"appnews": {"newsitems": [{"title": "Any News", "date": 1700000000}]}}
    if "GetTagList" in url:
        return {"response": {"tags": [{"name": "Roguelike", "tagid": 1716},
                                      {"name": "Co-op", "tagid": 3843}]}}
    if "GetRecentlyPlayedGames" in url:
        return {"response": {"games": [{"appid": 55, "name": "Recent X",
                                        "playtime_2weeks": 300}]}}
    return None


def main():
    tmp = Path(tempfile.mkdtemp())
    library.STATE = tmp
    library.DEALS = tmp / "deals.json"
    library.FACET_CACHE = tmp / "facet-cache.json"
    library.TAGMAP = tmp / "store-tags.json"
    library.PLAYHIST = tmp / "playtime-history.json"
    library._get = fake_get
    # Pin the secrets BEFORE anything runs: several fetchers reach for a key
    # (fetch_store_search -> _tag_map does). On a checkout that HAS a real
    # secrets.json this took the keyed path, cached a tag map, and then failed
    # the keyless assertion below - green on a bare worktree, red on the rig,
    # which is the worst way for a blind test to behave. Same answer anywhere.
    cglib.load_secrets = lambda: {}

    # --- specials: parsed, NOT_GAMES filtered, cents -> dollars --------------
    sp = library.fetch_specials()
    assert [s["appid"] for s in sp] == [1, 2], sp
    assert sp[0] == {"appid": 1, "name": "Special A", "discount": 25, "final": 14.99}, sp[0]

    # --- _store_items: name/price/discount, missing appids simply absent -----
    items = library._store_items([10, 11, 999])
    assert set(items) == {10, 11}, items
    assert items[10] == {"name": "Wish One", "final": "$5.00", "discount": 50, "price": 500}, items[10]
    # ...and CHUNKS past the 100-per-batch cap instead of truncating: id 12
    # sits at position 120, so it only prices if a second batch was fetched.
    big = [10] + list(range(900000, 900119)) + [12]
    assert set(library._store_items(big)) == {10, 12}, "the >100 tail was dropped"

    # --- wishlist_on_sale: only discounted, best deal first ------------------
    ws = library.fetch_wishlist_on_sale("7656119")
    assert [g["appid"] for g in ws] == [12, 10], ws     # 75% then 50%; 11 (0%) dropped
    assert ws[0]["discount"] == 75

    # --- trending: rank + name via GetItems ----------------------------------
    tr = library.fetch_trending()
    assert tr[0] == {"appid": 100, "rank": 1, "name": "Trend A"}, tr[0]
    assert tr[1]["rank"] == 2

    # --- store search: appids from capsule attrs (deduped) -> priced ---------
    rows = library.fetch_store_search(term="anything")
    assert [r["appid"] for r in rows] == [200, 201], rows
    # max_price clips on the AUTHORITATIVE GetItems price, not the search page
    clipped = library.fetch_store_search(term="anything", max_price=20)
    assert [r["appid"] for r in clipped] == [200], clipped   # 201 is $25 -> out

    # --- reviews: summary + non-empty snippets -------------------------------
    rv = library.fetch_reviews(1)
    assert rv["desc"] == "Very Positive" and rv["positive_pct"] == 90 and rv["total"] == 100, rv
    assert rv["snippets"] == ["great", "good"], rv          # the "" one dropped

    # --- news: patchnotes preferred, fallback to any -------------------------
    assert library.fetch_news(1)[0]["title"] == "Patch 1"

    # --- tag map: keyed only; fail-soft to {} without a key ------------------
    cglib.load_secrets = lambda: {}
    assert library._tag_map() == {}
    cglib.load_secrets = lambda: {"steamApiKey": "X" * 40, "steamId64": "7656119"}
    tmap = library._tag_map()
    assert tmap.get("roguelike") == 1716 and tmap.get("co-op") == 3843, tmap
    assert library.TAGMAP.exists()                          # cached to disk

    # Tag matching ignores punctuation and case, in BOTH directions: the model
    # says what a person says ("Rogue-like", "Co op"), Steam's vocabulary is
    # "Roguelike"/"Co-op". An exact lookup dropped "Rogue-like" and silently
    # widened the search to co-op-anything - which is how "a co-op roguelike
    # under $20" came back Dead by Daylight and Total War (2026-08-14). A
    # dropped filter is invisible in the results, so it gets a test.
    library.fetch_store_search(term="x", tags=["Rogue-like", "CO OP"])
    assert SEARCH_PARAMS[-1].get("tags") == "1716,3843", SEARCH_PARAMS[-1]
    library.fetch_store_search(term="x", tags=["Not A Real Tag"])
    assert "tags" not in SEARCH_PARAMS[-1], "unknown tags must drop, not 404"

    # --- hltb: fail-soft when the optional lib is absent, then cache hit -----
    # Force the import to FAIL regardless of the venv (requirements.txt pins
    # howlongtobeatpy, so the rig's venv HAS it - a None sys.modules entry
    # makes the import raise, keeping this offline everywhere): the raise ->
    # None (never crashes). A pre-seeded cache entry returns without importing.
    sys.modules["howlongtobeatpy"] = None
    assert library.fetch_hltb("Some Game With No Lib") is None
    library._save_facets({library.fuzzy_key("Hades"): {"hltb": {"main": 21}}})
    assert library.fetch_hltb("hades") == {"main": 21}      # cache hit, no import

    # --- refresh_deals: writes the one file the worker + list_games read -----
    library.refresh_deals()
    deals = library.load_deals()
    assert deals["specials"][0]["appid"] == 1
    assert [g["appid"] for g in deals["wishlist_on_sale"]] == [12, 10], deals
    assert "refreshed" in deals

    # --- playtime snapshot: one dated row, hours-only ------------------------
    library._snapshot_playtime({"1": {"hours": 10.5}, "2": {"hours": 0}})
    import time as _t
    hist = json.loads(library.PLAYHIST.read_text())
    today = _t.strftime("%Y-%m-%d")
    assert hist[today] == {"1": 10.5}, hist                 # 0-hour game omitted

    # --- recently played: needs a key; parsed 2-week hours -------------------
    rec = library.fetch_recently_played()
    assert rec == [{"appid": 55, "name": "Recent X", "hours2w": 5.0}], rec

    print("OK - layer 4: specials/wishlist/trending/search/reviews/news/tags/"
          "hltb-cache/refresh_deals/playtime/recent parsers")


if __name__ == "__main__":
    main()
