"""The Steam store and Web API, live: what the catalog cannot answer.

library.py is what the user OWNS - built from the PC's ACFs and the account's
playtime, cached in state/. This is everything asked of Steam itself in the
moment: deals, search, reviews, news, how-long-to-beat, trending, recently
played - the assistant's store tools and the worker's price facts - plus the
deals precompute both of them read. library.py's own layer 4 until it
outgrew a sitting (README's extraction rule (c)); the section comment below
is its original lead - it named this fault line before the file existed.

Shared with the catalog and imported BY it (one direction, no lazy import):
the two hosts, the name rule (ascii_only) and the not-a-game ids (NOT_GAMES).
Stdlib at import - requests only inside the fetchers - so the K15's system
python can import this beside library.py.

CLI (manual smoke test of live endpoint shapes a keyless checkout cannot see):
    python steamstore.py deals | search <terms> | reviews <appid>
                         | news <appid> | hltb <name> | trending | recent
"""
import json
import re
import sys
import time

import cglib
import statefile

STATE = statefile.STATE
log = cglib.make_log("library")     # same Loki lane as the catalog: one small
#                                     fixed set of lane labels (cglib.make_log)

# --- layer 4: store questions (deals, search, reviews, news, hltb) ------------
# Live store/Web-API queries that answer "what should I play or buy" rather than
# "what do I own". Same fail-soft idioms as layers 2-3, but every fetch routes
# through _get so a test swaps ONE seam (the couch.ssh pattern in dispatch.py).
# This whole section was library.py's pre-drawn fault line for steamstore.py,
# split out as a pure move once it outgrew a sitting (README's rule (c)).
#
# Endpoint-shape confidence, since these are undocumented-but-stable and we
# cannot see them from a keyless checkout: /appreviews and GetNewsForApp are the
# only officially-documented ones. GetItems, GetWishlist, featuredcategories,
# GetMostPlayedGames and /search/results are community-stable - so the parses
# below read defensively (.get chains, tolerate missing keys) and the whole
# lane degrades to a spoken "couldn't reach the store", never a crash. The live
# smoke test in the bring-up guide is what confirms the real shapes on the rig.

# The two Steam hosts, named once: every fetcher here and library.py's
# layers 2-3 talk to one of them.
STORE = "https://store.steampowered.com"
API = "https://api.steampowered.com"

DEALS = STATE / "deals.json"                # wishlist-on-sale + specials snapshot
DEALS_MAX_AGE_S = 6 * 3600                  # prices move at sale boundaries
FACET_CACHE = STATE / "facet-cache.json"    # per-game how-long-to-beat (stable)
TAGMAP = STATE / "store-tags.json"          # {tag_name_lower: tagid}, weekly
TAGMAP_MAX_AGE_S = 7 * 24 * 3600
NOT_GAMES = {228980}                        # Steamworks Common Redistributables


def _get(url, params=None, timeout=20):
    """The one HTTP seam for layer 4 - tests replace this to feed canned JSON.
    Returns parsed JSON or None (never raises): a store hiccup must degrade a
    voice answer to "couldn't reach the store", never take down a tool call."""
    import requests
    try:
        r = requests.get(url, params=params or {}, timeout=timeout,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except Exception as e:                  # network, non-2xx, or non-JSON body
        log.warn("store_fetch_failed", url=url.rsplit("/", 1)[-1] or url, err=str(e))
        return None


_CC = None


def _cc():
    """Country code for prices, from config.location.country (defaults US).
    One home, so every price call is in the same currency the assistant quotes.
    Read once per process: it used to re-read and re-parse config.json from
    disk on every store call - one read per 100-item chunk of a wishlist
    refresh - for a value that only changes with a restart anyway."""
    global _CC
    if _CC is None:
        try:
            _CC = (cglib.load_config().get("voice", {})
                   .get("location", {}).get("country") or "US").upper()
        except Exception:
            _CC = "US"
    return _CC


def ascii_only(s):
    """Names go ASCII-only, the same rule the catalog fetchers use (encoding-
    proof across every hop, and (tm)-glyphs are noise to voice)."""
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def store_items(appids, cc=None):
    """IStoreBrowseService/GetItems - the batch name/price/discount workhorse,
    keyless. {appid: {name, price, discount, final}} for the appids it could
    resolve; missing ones are simply absent (fail-soft per item). Review scores
    are deliberately NOT here: GetItems returns no review block (only an ESRB
    game_rating), so per-game sentiment comes from fetch_reviews instead - one
    call per title, too many to fold into a batch."""
    appids = [int(a) for a in appids if int(a) not in NOT_GAMES]
    if not appids:
        return {}
    cc = cc or _cc()
    out = {}
    # GetItems caps a batch at 100 - CHUNK past it rather than truncate, or a
    # 100+ wishlist silently loses every deal past the cap. Only refresh_deals
    # (off-turn) ever sends more than one chunk; in-turn callers pass <=20.
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
                "name": ascii_only(it.get("name", "")),
                "final": opt.get("formatted_final_price"),
                "discount": int(opt.get("discount_pct", 0) or 0),
                "price": (int(opt.get("final_price_in_cents", 0) or 0) or None),
            }
    return out


def fetch_wishlist_on_sale(steamid, cc=None):
    """Keyless IWishlistService/GetWishlist -> GetItems for prices -> the ones
    actually discounted, best deal first."""
    d = _get(f"{API}/IWishlistService/GetWishlist/v1/", {"steamid": steamid})
    items = ((d or {}).get("response", {}) or {}).get("items", []) or []
    # int() the appids: GetItems keys its result by int, so a string appid from
    # the API would silently miss the price lookup and drop the game.
    appids = [int(it["appid"]) for it in items if it.get("appid")]
    priced = store_items(appids, cc)
    on_sale = [{"appid": a, **priced[a]} for a in appids
               if a in priced and priced[a]["discount"] > 0]
    on_sale.sort(key=lambda g: -g["discount"])
    return on_sale


def fetch_specials(cc=None):
    """Front-page specials feed (featuredcategories). Curated, ~a couple dozen -
    the "what's on sale right now" starter, not an exhaustive sale list."""
    d = _get(f"{STORE}/api/featuredcategories",
             {"cc": cc or _cc(), "l": "english"})
    items = ((d or {}).get("specials", {}) or {}).get("items", []) or []
    out = []
    for it in items:
        if it.get("id") in NOT_GAMES:
            continue
        out.append({"appid": it.get("id"), "name": ascii_only(it.get("name", "")),
                    "discount": int(it.get("discount_percent", 0) or 0),
                    "final": (it.get("final_price", 0) or 0) / 100 or None})
    return out


def fetch_trending(cc=None):
    """ISteamChartsService/GetMostPlayedGames -> names via GetItems. "What's
    everyone playing" - keyless, top by concurrent players."""
    d = _get(f"{API}/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response", {}) or {}).get("ranks", []) or []
    appids = [int(r["appid"]) for r in ranks[:20] if r.get("appid")]   # int keys, see wishlist
    named = store_items(appids, cc)
    return [{"appid": a, "rank": i + 1,
             "name": named.get(a, {}).get("name") or f"app {a}"}
            for i, a in enumerate(appids)]


def fetch_recently_played():
    """GetRecentlyPlayedGames (last two weeks) - the honest "what have I been
    playing". Own key only; empty without it. Two weeks is the whole window
    Steam offers; a longer one would need daily playtime snapshots kept here,
    which is a real feature and not a line to leave lying around unused."""
    s = cglib.load_secrets()
    if not (cglib.real_key(s.get("steamApiKey")) and str(s.get("steamId64", "")).isdigit()):
        return []
    d = _get(f"{API}/IPlayerService/GetRecentlyPlayedGames/v1/",
             {"key": s["steamApiKey"], "steamid": s["steamId64"]})
    games = ((d or {}).get("response", {}) or {}).get("games", []) or []
    return [{"appid": g.get("appid"), "name": ascii_only(g.get("name", "")),
             "hours2w": round(g.get("playtime_2weeks", 0) / 60, 1)}
            for g in games if g.get("appid")]


def _tag_map():
    """{tag_name_lower: tagid} for turning a spoken genre into a search filter.
    Cached weekly; keyless GetTagList is unavailable, so this needs the key and
    fail-softs to {} (search then falls back to term-only)."""
    try:
        fresh = time.time() - TAGMAP.stat().st_mtime < TAGMAP_MAX_AGE_S
    except OSError:                             # missing or a stat race -> refetch
        fresh = False
    if fresh:
        cached = statefile.load_json(TAGMAP)
        if cached:
            return cached
    s = cglib.load_secrets()
    if not cglib.real_key(s.get("steamApiKey")):
        return {}
    d = _get(f"{API}/IStoreService/GetTagList/v1/", {"key": s["steamApiKey"],
                                                     "language": "english"})
    tags = ((d or {}).get("response", {}) or {}).get("tags", []) or []
    out = {ascii_only(t.get("name", "")).lower(): t.get("tagid")
           for t in tags if t.get("name") and t.get("tagid")}
    if out:
        statefile.atomic_write(TAGMAP, out)
    return out


def fetch_store_search(term="", tags=None, max_price=None, on_sale=False, cc=None):
    """"Find me a co-op roguelike under $20 [on sale]". Keyless /search/results
    for the filtered appid list, then GetItems for names/prices. Tag names ->
    tagids via the cached map; unknown tags are dropped (term still applies)."""
    # Match tags on letters and digits only. The model says what a person says
    # ("Rogue-like", "Co op"); Steam's vocabulary is "Roguelike", "Co-op". An
    # exact lookup dropped "Rogue-like" silently, leaving a co-op-only search
    # that answered "a co-op roguelike under $20" with Dead by Daylight and
    # Total War (2026-08-14) - a dropped filter is invisible in the result.
    tmap = {fuzzy_key(k): v for k, v in _tag_map().items()}
    tagids = [str(tmap[fuzzy_key(t)]) for t in (tags or []) if fuzzy_key(t) in tmap]
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
        if a not in seen and a not in NOT_GAMES:
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
    """Official /appreviews summary + a few recent snippets. The "what are people
    saying about X / the new DLC" answer (query the DLC's own appid for that)."""
    d = _get(f"{STORE}/appreviews/{int(appid)}",
             {"json": 1, "language": "english", "filter": "recent",
              "num_per_page": 5, "purchase_type": "all"})
    if not d or not d.get("query_summary"):
        return None
    q = d["query_summary"]
    snippets = [ascii_only(r.get("review", ""))[:280]
                for r in (d.get("reviews") or [])[:3] if r.get("review")]
    return {"desc": q.get("review_score_desc"),
            "positive_pct": (round(100 * q.get("total_positive", 0)
                                   / q["total_reviews"])
                             if q.get("total_reviews") else None),
            "total": q.get("total_reviews"), "snippets": snippets}


def fetch_news(appid, count=3):
    """Official GetNewsForApp, patch notes preferred. "Any updates for X?" -
    keyless, titles only (URLs are unspeakable and dropped)."""
    d = _get(f"{API}/ISteamNews/GetNewsForApp/v2/",
             {"appid": int(appid), "count": count, "maxlength": 1,
              "tags": "patchnotes"})
    items = ((d or {}).get("appnews", {}) or {}).get("newsitems", []) or []
    if not items:                           # no patch notes -> any announcement
        d = _get(f"{API}/ISteamNews/GetNewsForApp/v2/",
                 {"appid": int(appid), "count": count, "maxlength": 1})
        items = ((d or {}).get("appnews", {}) or {}).get("newsitems", []) or []
    return [{"title": ascii_only(n.get("title", "")),
             "date": time.strftime("%Y-%m-%d", time.localtime(n.get("date", 0)))}
            for n in items[:count] if n.get("title")]


def fetch_hltb(name):
    """How-long-to-beat hours via the maintained howlongtobeatpy (it chases
    HLTB's endpoint churn for us). Lazy + fail-soft on BOTH the import and the
    call: the library is an optional pin (it drags in fake_useragent/bs4), and
    beat-times never move, so results cache forever in FACET_CACHE."""
    key = fuzzy_key(name)
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


def fuzzy_key(name):
    """A name or store tag reduced to letters and digits, so spoken/model
    spellings meet Steam's: rogue-like == Roguelike, 'co op' == Co-op - and
    the key the facet cache files a title under. (It had a twin, _tagkey,
    with the identical body.)"""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# -- caches ------------------------------------------------------------------
# Read and written through statefile (load_json / atomic_write - tmp +
# os.replace), so a JSON write is never half-flushed on a crash.

def _load_facets():
    return statefile.load_json(FACET_CACHE)


def _save_facets(cache):
    statefile.atomic_write(FACET_CACHE, cache)


def load_deals():
    return statefile.load_json(DEALS)


def refresh_deals():
    """Precompute the feed answers into state/deals.json - list_games reads it
    in ~0 ms, and worker_home reads the SAME file for grounded facts (no shell,
    no re-scrape; AGENTS.md points at it). Own key only for the wishlist half;
    specials is keyless, so a keyless rig still gets the sale feed."""
    s = cglib.load_secrets()
    steamid = str(s.get("steamId64", ""))
    specials = fetch_specials()
    wishlist = fetch_wishlist_on_sale(steamid) if steamid.isdigit() else []
    # Both empty almost always means the store was unreachable, not that there
    # is genuinely nothing on sale (specials is never empty on a live Steam).
    # Don't stamp a poisoned snapshot: the staleness gate would then serve empty
    # for DEALS_MAX_AGE_S. Skip, and the next sync retries - like refresh_owned,
    # which also writes no stamp on failure.
    if not specials and not wishlist:
        log.warn("sync_skipped", layer="deals", reason="no data (store unreachable?)")
        return 1
    deals = {"refreshed": time.strftime("%Y-%m-%dT%H:%M:%S"), "specials": specials}
    if steamid.isdigit():
        deals["wishlist_on_sale"] = wishlist
    statefile.atomic_write(DEALS, deals)
    log("deals_synced", specials=len(specials), wishlist=len(wishlist))
    return 0


def probe(args):
    """Manual smoke test - the one that confirms live endpoint shapes a
    keyless checkout cannot see. `python steamstore.py deals|search <terms>|
    reviews <appid>|news <appid>|hltb <name>|trending|recent`."""
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
    print("usage: steamstore.py deals | search <terms> | reviews <appid> | "
          "news <appid> | hltb <name> | trending | recent")
    return 2


if __name__ == "__main__":
    sys.exit(probe(sys.argv[1:]))
