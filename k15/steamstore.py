"""Live Steam store data: deals, search, reviews, news, how-long-to-beat,
and the state/deals.json precompute. Layer 4 of the catalog - library.py
builds layers 1-3, what the user owns and has installed. Every fetch
routes through _get, the one test seam. Only /appreviews and GetNewsForApp
are officially documented, so parses read defensively and the lane degrades
rather than crashing.

CLI:
    python steamstore.py <deals|search ...|reviews <appid>|news <appid>
                          |hltb <name>|trending|recent>
"""
import json
import re
import sys
import time

import cglib
import library

# The lane is a Loki label: store events ship as "library" (test_event_names
# pins the set).
log = cglib.make_log("library")

STORE = "https://store.steampowered.com"
API = "https://api.steampowered.com"

DEALS = cglib.STATE / "deals.json"                # wishlist-on-sale + specials snapshot
DEALS_MAX_AGE_S = 6 * 3600                  # prices move at sale boundaries
FACET_CACHE = cglib.STATE / "facet-cache.json"    # per-game how-long-to-beat (stable)
TAGMAP = cglib.STATE / "store-tags.json"          # {tag_name_lower: tagid}, weekly
TAGMAP_MAX_AGE_S = 7 * 24 * 3600


def _get(url, params=None, timeout=20):
    """One HTTP seam for layer 4 - tests swap it. JSON or None, never raises."""
    import requests
    try:
        r = requests.get(url, params=params or {}, timeout=timeout,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except Exception as e:                  # network, non-2xx, or non-JSON body
        log.warn("store_fetch_failed", url=url.rsplit("/", 1)[-1] or url, err=str(e))
        return None


def _cc():
    """Country code for prices, from voice.location.country (defaults US)."""
    try:
        return (cglib.config().get("voice", {})
                .get("location", {}).get("country") or "US").upper()
    except Exception:
        return "US"


def store_items(appids, cc=None):
    """GetItems - batch name/price/discount, keyless. Unresolved appids are
    simply absent. No review scores: GetItems returns no review block (only an
    ESRB game_rating), so sentiment comes from fetch_reviews."""
    appids = [int(a) for a in appids if int(a) not in library.NOT_GAMES]
    if not appids:
        return {}
    cc = cc or _cc()
    out = {}
    # GetItems caps a batch at 100 - chunk, or a 100+ wishlist loses deals.
    for i in range(0, len(appids), 100):
        body = {
            "ids": [{"appid": a} for a in appids[i:i + 100]],
            "context": {"language": "english", "country_code": cc,
                        "steam_realm": 1},
            "data_request": {"include_all_purchase_options": True},
        }
        d = _get(f"{API}/IStoreBrowseService/GetItems/v1/",
                 {"input_json": json.dumps(body)})
        for it in ((d or {}).get("response", {}) or {}).get("store_items", []) or []:
            appid = it.get("appid") or it.get("id")
            if not appid:
                continue
            opt = it.get("best_purchase_option", {}) or {}
            out[int(appid)] = {
                "name": library.ascii_only(it.get("name", "")),
                "final": opt.get("formatted_final_price"),
                "discount": int(opt.get("discount_pct", 0) or 0),
                "price": (int(opt.get("final_price_in_cents", 0) or 0) or None),
            }
    return out


def fetch_wishlist_on_sale(steamid, cc=None):
    """Keyless GetWishlist -> GetItems -> the discounted ones, best first."""
    d = _get(f"{API}/IWishlistService/GetWishlist/v1/", {"steamid": steamid})
    items = ((d or {}).get("response", {}) or {}).get("items", []) or []
    # GetItems keys by int; a string appid would silently drop the game.
    appids = [int(it["appid"]) for it in items if it.get("appid")]
    priced = store_items(appids, cc)
    on_sale = [{"appid": a, **priced[a]} for a in appids
               if a in priced and priced[a]["discount"] > 0]
    on_sale.sort(key=lambda g: -g["discount"])
    return on_sale


def fetch_specials(cc=None):
    """Front-page specials feed. Curated, ~a couple dozen, not exhaustive."""
    d = _get(f"{STORE}/api/featuredcategories",
             {"cc": cc or _cc(), "l": "english"})
    items = ((d or {}).get("specials", {}) or {}).get("items", []) or []
    out = []
    for it in items:
        if it.get("id") in library.NOT_GAMES:
            continue
        out.append({"appid": it.get("id"), "name": library.ascii_only(it.get("name", "")),
                    "discount": int(it.get("discount_percent", 0) or 0),
                    "final": (it.get("final_price", 0) or 0) / 100 or None})
    return out


def fetch_trending(cc=None):
    """GetMostPlayedGames -> names via GetItems. Keyless, by concurrents."""
    d = _get(f"{API}/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response", {}) or {}).get("ranks", []) or []
    appids = [int(r["appid"]) for r in ranks[:20] if r.get("appid")]   # int keys, see wishlist
    named = store_items(appids, cc)
    return [{"appid": a, "rank": i + 1,
             "name": named.get(a, {}).get("name") or f"app {a}"}
            for i, a in enumerate(appids)]


def fetch_recently_played():
    """GetRecentlyPlayedGames. Own key only. Two weeks is all Steam offers."""
    creds = library.steam_creds()
    if not creds:
        return []
    d = _get(f"{API}/IPlayerService/GetRecentlyPlayedGames/v1/",
             {"key": creds[0], "steamid": creds[1]})
    games = ((d or {}).get("response", {}) or {}).get("games", []) or []
    return [{"appid": g.get("appid"), "name": library.ascii_only(g.get("name", "")),
             "hours2w": round(g.get("playtime_2weeks", 0) / 60, 1)}
            for g in games if g.get("appid")]


def _tag_map():
    """{tag_name_lower: tagid} for turning a spoken genre into a search filter.
    Cached weekly; GetTagList needs the key, else {} and search goes term-only."""
    try:
        fresh = time.time() - TAGMAP.stat().st_mtime < TAGMAP_MAX_AGE_S
    except OSError:                             # missing or a stat race -> refetch
        fresh = False
    if fresh:
        cached = cglib.load_json(TAGMAP, None)
        if cached is not None:
            return cached
    s = cglib.load_secrets()
    if not cglib.real_key(s.get("steamApiKey")):
        return {}
    d = _get(f"{API}/IStoreService/GetTagList/v1/", {"key": s["steamApiKey"],
                                                     "language": "english"})
    tags = ((d or {}).get("response", {}) or {}).get("tags", []) or []
    out = {library.ascii_only(t.get("name", "")).lower(): t.get("tagid")
           for t in tags if t.get("name") and t.get("tagid")}
    if out:
        cglib.write_json(TAGMAP, out, indent=1)
    return out


def fetch_store_search(term="", tags=None, max_price=None, on_sale=False, cc=None):
    """Keyless /search/results for the appid list, then GetItems for names and
    prices. Tag names -> tagids via the cached map; unknown tags are dropped."""
    # An exact tag lookup dropped "Rogue-like" silently (2026-08-14).
    tmap = {library.fuzzy_key(k): v for k, v in _tag_map().items()}
    tagids = [str(tmap[library.fuzzy_key(t)]) for t in (tags or []) if library.fuzzy_key(t) in tmap]
    params = {"term": term or "", "cc": cc or _cc(), "l": "english",
              "count": 50, "infinite": 1, "json": 1}
    if tagids:
        params["tags"] = ",".join(tagids)
    if max_price:
        params["maxprice"] = int(max_price)
    if on_sale:
        params["specials"] = 1
    d = _get(f"{STORE}/search/results/", params)
    html = (d or {}).get("results_html", "") or ""
    seen, appids = set(), []
    for m in re.finditer(r'data-ds-appid="(\d+)"', html):   # capsule attr only
        a = int(m.group(1))
        if a not in seen and a not in library.NOT_GAMES:
            seen.add(a); appids.append(a)
        if len(appids) >= 20:
            break
    named = store_items(appids, cc)
    rows = [{"appid": a, **named[a]} for a in appids if a in named]
    if max_price:                           # GetItems is truth on price; re-clip
        cap = int(max_price) * 100
        rows = [r for r in rows if not r.get("price") or r["price"] <= cap]
    return rows[:12]


def fetch_reviews(appid):
    """/appreviews summary + recent snippets. For DLC, pass the DLC's appid."""
    d = _get(f"{STORE}/appreviews/{int(appid)}",
             {"json": 1, "language": "english", "filter": "recent",
              "num_per_page": 5, "purchase_type": "all"})
    if not d or not d.get("query_summary"):
        return None
    q = d["query_summary"]
    snippets = [library.ascii_only(r.get("review", ""))[:280]
                for r in (d.get("reviews") or [])[:3] if r.get("review")]
    return {"desc": q.get("review_score_desc"),
            "positive_pct": (round(100 * q.get("total_positive", 0)
                                   / q["total_reviews"])
                             if q.get("total_reviews") else None),
            "total": q.get("total_reviews"), "snippets": snippets}


def fetch_news(appid, count=3):
    """GetNewsForApp, patch notes preferred. Keyless, titles only."""
    d = _get(f"{API}/ISteamNews/GetNewsForApp/v2/",
             {"appid": int(appid), "count": count, "maxlength": 1,
              "tags": "patchnotes"})
    items = ((d or {}).get("appnews", {}) or {}).get("newsitems", []) or []
    if not items:                           # no patch notes -> any announcement
        d = _get(f"{API}/ISteamNews/GetNewsForApp/v2/",
                 {"appid": int(appid), "count": count, "maxlength": 1})
        items = ((d or {}).get("appnews", {}) or {}).get("newsitems", []) or []
    return [{"title": library.ascii_only(n.get("title", "")),
             "date": time.strftime("%Y-%m-%d", time.localtime(n.get("date", 0)))}
            for n in items[:count] if n.get("title")]


def fetch_hltb(name):
    """Beat times via howlongtobeatpy, which chases howlongtobeat.com's
    endpoint churn. Lazy + fail-soft on BOTH import and call (the pin is
    optional, it drags in fake_useragent/bs4). Beat-times never move, so
    results cache forever."""
    key = library.fuzzy_key(name)
    cache = _load_facets()
    if key in cache and "hltb" in cache[key]:
        return cache[key]["hltb"]
    hltb = None
    try:
        from howlongtobeatpy import HowLongToBeat
        best = None
        for e in (HowLongToBeat().search(name) or []):
            if best is None or (e.similarity or 0) > (best.similarity or 0):
                best = e
        if best:
            hltb = {"main": best.main_story, "extra": best.main_extra,
                    "complete": best.completionist}
    except Exception as e:                  # missing pin, or endpoint churn
        log.warn("hltb_failed", name=name[:80], err=str(e))
        return None
    cache.setdefault(key, {})["hltb"] = hltb
    _save_facets(cache)
    return hltb


def _load_facets():
    return cglib.load_json(FACET_CACHE, {})


def _save_facets(cache):
    cglib.write_json(FACET_CACHE, cache, indent=1)


def load_deals():
    return cglib.load_json(DEALS, {})


def refresh_deals():
    """Precompute the feed answers into state/deals.json, read by list_games
    and worker_home. The wishlist half needs the key; specials is keyless."""
    s = cglib.load_secrets()
    steamid = str(s.get("steamId64", ""))
    specials = fetch_specials()
    wishlist = fetch_wishlist_on_sale(steamid) if steamid.isdigit() else []
    # Both empty means the store was unreachable (specials is never empty on a
    # live Steam); stamping it would serve empty for DEALS_MAX_AGE_S.
    if not specials and not wishlist:
        log.warn("sync_skipped", layer="deals", reason="no data (store unreachable?)")
        return 1
    deals = {"refreshed": time.strftime("%Y-%m-%dT%H:%M:%S"), "specials": specials}
    if steamid.isdigit():
        deals["wishlist_on_sale"] = wishlist
    cglib.write_json(DEALS, deals, indent=1)
    log("deals_synced", specials=len(specials), wishlist=len(wishlist))
    return 0


def probe(args):
    """Manual layer-4 smoke test: confirms live endpoint shapes a keyless
    checkout cannot see. Verbs as in usage()."""
    what = args[0] if args else "deals"
    if what == "deals":
        refresh_deals(); out = load_deals()
    elif what == "search":
        out = fetch_store_search(term=" ".join(args[1:]))
    elif what == "reviews":
        out = fetch_reviews(args[1])
    elif what == "news":
        out = fetch_news(args[1])
    elif what == "hltb":
        out = fetch_hltb(" ".join(args[1:]))
    elif what == "trending":
        out = fetch_trending()
    elif what == "recent":
        out = fetch_recently_played()
    else:
        return usage()
    print(json.dumps(out, indent=2))
    return 0


def usage():
    print("usage: steamstore.py <deals|search ...|reviews <appid>|news <appid>"
          "|hltb <name>|trending|recent>")
    return 2


if __name__ == "__main__":
    sys.exit(probe(sys.argv[1:]))
