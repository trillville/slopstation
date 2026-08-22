"""Game-library index builder.

Layer 1: installed games - `ssh gamepc games` (the Dispatch verb) when run on
the K15, or --local-steam (direct ACF scan) on the gaming PC itself. Layers 2-3
(owned/playtime via Steam Web API, metadata via appdetails + SteamSpy) merge
into the same file.

Output: state/library.json  {"refreshed": iso-utc, "installed": [rows]}
written atomically (tmp + os.replace). Consumers: Flux keyterms, the grammar's
{game} slot, fuzzy launch resolution, and the assistant's catalog.

Layer 4 (the store questions - deals, search, reviews, news, how-long-to-beat)
is live Steam data rather than the catalog, and has its own section below.

The voice agent calls sync() on a background thread at startup and after each
session - installed refreshes when the PC is awake, deals are keyless, and
owned/metadata run whenever a Steam key is present. The CLI verbs below are for
manual/dev use.

CLI:
    python library.py sync                        (every layer, as the agent does)
    python library.py refresh [--local-steam] [--owned] [--meta [N]]
    python library.py show
    python library.py catalog
    python library.py probe <deals|search ...|reviews <appid>|news <appid>
                             |hltb <name>|trending|recent>
"""
import glob
import json
import re
import sys
import threading
import time
from pathlib import Path

import cglib
import couch
import statefile
# couch.ssh is reached through the MODULE (one seam - dispatch.py says why),
# and at the top now that importing couch no longer reads config.json.

STATE = statefile.STATE
LIBRARY = STATE / "library.json"

log = cglib.make_log("library")


# --- layer 1 sources ----------------------------------------------------------


def fetch_installed_ssh():
    """The production path (K15): the gaming PC enumerates its own ACFs."""
    return parse_games_json(couch.ssh("games", timeout=30))


def parse_games_json(text):
    rows = json.loads(text.strip().lstrip("﻿"))
    if isinstance(rows, dict):          # single-game library edge
        rows = [rows]
    return rows


def fetch_installed_local():
    """Running ON the gaming PC: same fields, no ssh. Mirrors the Dispatch
    `games` verb so blind tests validate both against each other."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
        steam = winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    roots = {steam.lower()}
    lf = Path(steam) / "steamapps" / "libraryfolders.vdf"
    if lf.exists():
        for m in re.finditer(r'^\s*"path"\s+"(.+)"\s*$', lf.read_text(), re.M):
            roots.add(m.group(1).replace("\\\\", "\\").lower())
    rows, seen = [], set()
    for root in roots:
        for acf in glob.glob(str(Path(root) / "steamapps" / "appmanifest_*.acf")):
            text = Path(acf).read_text(encoding="utf-8", errors="replace")
            f = {}
            for key in ("appid", "name", "StateFlags", "SizeOnDisk", "LastPlayed"):
                m = re.search(f'"{key}"\\s+"([^"]*)"', text)
                if m:
                    f[key] = m.group(1)
            # ASCII-only names, same rule as the Dispatch verb (encoding-proof
            # across ssh/shell hops; (tm) glyphs are noise to voice anyway).
            name = _ascii(f.get("name", ""))
            if f.get("appid") and name and f["appid"] not in seen:
                seen.add(f["appid"])
                rows.append({"appid": int(f["appid"]), "name": name,
                             "state": int(f.get("StateFlags", 0) or 0),
                             "size": int(f.get("SizeOnDisk", 0) or 0),
                             "lastPlayed": int(f.get("LastPlayed", 0) or 0)})
    return rows


# --- index file ---------------------------------------------------------------


def load():
    return statefile.load_json(LIBRARY)


def installed_name(appid):
    """Installed title for an appid, or None. The one home for the lookup:
    the assistant's tools and dispatch's BUSY message both name games with
    it, and load() already fail-softs to {} on a missing/corrupt index."""
    for r in load().get("installed", []):
        if r["appid"] == appid:
            return r["name"]
    return None


def save(index):
    statefile.atomic_write(LIBRARY, index)


def refresh(local=False):
    try:
        rows = fetch_installed_local() if local else fetch_installed_ssh()
    except Exception as e:
        log.warn("sync_skipped", layer="installed", err=str(e))
        return 1
    index = load()
    index["refreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    index["installed"] = rows
    save(index)
    log("sync_done", layer="installed", games=len(rows))
    return 0


def fetch_collections_ssh():
    """The PC's Big Picture collections as [{name, id}] - the `collections`
    Dispatch verb reads them from the cloud-storage file. K15-only (needs the
    PC awake), same as installed."""
    return parse_games_json(couch.ssh("collections", timeout=15))


def refresh_collections():
    """Sync collection name->id into the index so the voice grammar can resolve
    'show my roguelikes'. PC-dependent, fail-soft when asleep (the whole point
    of the layer-1 pattern)."""
    try:
        rows = fetch_collections_ssh()
    except Exception as e:
        log.warn("sync_skipped", layer="collections", err=str(e))
        return 1
    index = load()
    index["collections"] = rows
    save(index)
    log("sync_done", layer="collections", n=len(rows))
    return 0


def show():
    index = load()
    rows = sorted(index.get("installed", []),
                  key=lambda r: r.get("lastPlayed", 0), reverse=True)
    if not rows:
        print("no index - run: python library.py refresh")
        return 1
    print(f"refreshed {index.get('refreshed', '?')} - {len(rows)} installed")
    for r in rows:
        last = (time.strftime("%Y-%m-%d", time.localtime(r["lastPlayed"]))
                if r.get("lastPlayed") else "never")
        print(f"  {r['appid']:>8}  {last}  {r['name']}")
    return 0


# --- layers 2-3: owned/playtime + metadata ------------------------------------

META_CACHE = STATE / "metadata-cache.json"
_CTRL = {28: "full", 18: "partial"}          # Steam category ids


def fetch_owned(api_key, steamid):
    """Steam Web API: account-global playtime + recency (the canonical
    source - ACF LastPlayed is per-machine). One call, own key only."""
    import requests
    r = requests.get(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
        params={"key": api_key, "steamid": steamid, "include_appinfo": 1,
                "include_played_free_games": 1, "format": "json"}, timeout=30)
    r.raise_for_status()
    out = {}
    for g in r.json().get("response", {}).get("games", []):
        out[str(g["appid"])] = {
            "hours": round(g.get("playtime_forever", 0) / 60, 1),
            "hours2w": round(g.get("playtime_2weeks", 0) / 60, 1),
            "last": g.get("rtime_last_played", 0),
            "name": _ascii(g.get("name", "")),
        }
    return out


def fetch_meta_one(appid):
    """appdetails (genres/controller/desc/score/year) + SteamSpy tags.
    Caller paces requests; results are cached forever."""
    import requests
    meta = {}
    r = requests.get("https://store.steampowered.com/api/appdetails",
                     params={"appids": appid}, timeout=20)
    d = r.json().get(str(appid), {})
    if d.get("success"):
        data = d["data"]
        cats = {c["id"] for c in data.get("categories", [])}
        meta.update({
            "genres": [g["description"] for g in data.get("genres", [])],
            "controller": next((v for k, v in _CTRL.items() if k in cats), "none"),
            "desc": re.sub(r"[^\x20-\x7E]", "",
                           data.get("short_description", ""))[:160],
            "score": (data.get("metacritic") or {}).get("score"),
            "year": (data.get("release_date") or {}).get("date", "")[-4:],
        })
    r2 = requests.get("https://steamspy.com/api.php",
                      params={"request": "appdetails", "appid": appid}, timeout=20)
    tags = r2.json().get("tags") or {}
    if isinstance(tags, dict):
        meta["tags"] = [t for t, _ in
                        sorted(tags.items(), key=lambda kv: -kv[1])[:10]]
    return meta


def load_meta():
    return statefile.load_json(META_CACHE)


def _save_meta(cache):
    statefile.atomic_write(META_CACHE, cache)


def refresh_meta(appids, limit=200):
    """Top up NEW appids only, ~1 req/2 s (appdetails' unofficial ceiling).
    Saves after EACH fetch: the crawl runs on a daemon thread that dies with
    the agent, so a batched save would re-crawl from zero every restart."""
    cache = load_meta()
    todo = [a for a in appids if str(a) not in cache][:limit]
    for i, appid in enumerate(todo):
        try:
            cache[str(appid)] = fetch_meta_one(appid)
            _save_meta(cache)
            log("meta_fetched", appid=appid, n=i + 1, of=len(todo))
        except Exception as e:
            log.warn("meta_failed", appid=appid, err=str(e))
        time.sleep(2.1)
    return cache


def refresh_owned():
    s = cglib.load_secrets()
    if not (cglib.real_key(s.get("steamApiKey")) and str(s.get("steamId64", "")).isdigit()):
        log("sync_skipped", layer="owned", reason="steamApiKey/steamId64 not set")
        return 1
    index = load()
    index["owned"] = fetch_owned(s["steamApiKey"], s["steamId64"])
    index["ownedRefreshed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save(index)
    log("sync_done", layer="owned", games=len(index["owned"]))
    return 0


# --- layer 4: store questions (deals, search, reviews, news, hltb) ------------
# Live store/Web-API queries that answer "what should I play or buy" rather than
# "what do I own". Same fail-soft idioms as layers 2-3, but every fetch routes
# through _get so a test swaps ONE seam (the couch.ssh pattern in dispatch.py).
# This whole section is the pre-drawn fault line for a future steamstore.py:
# split it out as a pure move if it outgrows a sitting, not before.
#
# Endpoint-shape confidence, since these are undocumented-but-stable and we
# cannot see them from a keyless checkout: /appreviews and GetNewsForApp are the
# only officially-documented ones. GetItems, GetWishlist, featuredcategories,
# GetMostPlayedGames and /search/results are community-stable - so the parses
# below read defensively (.get chains, tolerate missing keys) and the whole
# lane degrades to a spoken "couldn't reach the store", never a crash. The live
# smoke test in the bring-up guide is what confirms the real shapes on the rig.

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


def _cc():
    """Country code for prices, from config.location.country (defaults US).
    One home, so every price call is in the same currency the assistant quotes."""
    try:
        return (cglib.load_config().get("voice", {})
                .get("location", {}).get("country") or "US").upper()
    except Exception:
        return "US"


def _ascii(s):
    """Names go ASCII-only, the same rule the catalog fetchers use (encoding-
    proof across every hop, and (tm)-glyphs are noise to voice)."""
    return re.sub(r"[^\x20-\x7E]", "", s or "").strip()


def _tagkey(s):
    """A store tag reduced to letters and digits, so spoken/model spellings
    meet Steam's: rogue-like == Roguelike, 'co op' == Co-op."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _store_items(appids, cc=None):
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
                "name": _ascii(it.get("name", "")),
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
    priced = _store_items(appids, cc)
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
        out.append({"appid": it.get("id"), "name": _ascii(it.get("name", "")),
                    "discount": int(it.get("discount_percent", 0) or 0),
                    "final": (it.get("final_price", 0) or 0) / 100 or None})
    return out


def fetch_trending(cc=None):
    """ISteamChartsService/GetMostPlayedGames -> names via GetItems. "What's
    everyone playing" - keyless, top by concurrent players."""
    d = _get(f"{API}/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response", {}) or {}).get("ranks", []) or []
    appids = [int(r["appid"]) for r in ranks[:20] if r.get("appid")]   # int keys, see wishlist
    named = _store_items(appids, cc)
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
    return [{"appid": g.get("appid"), "name": _ascii(g.get("name", "")),
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
    out = {_ascii(t.get("name", "")).lower(): t.get("tagid")
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
    tmap = {_tagkey(k): v for k, v in _tag_map().items()}
    tagids = [str(tmap[_tagkey(t)]) for t in (tags or []) if _tagkey(t) in tmap]
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
    named = _store_items(appids, cc)
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
    snippets = [_ascii(r.get("review", ""))[:280]
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
    return [{"title": _ascii(n.get("title", "")),
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


# --- the sync orchestrator (all three layers, staleness- and key-gated) -------
# Layer 1 (installed) needs the PC awake and fail-softs when it is asleep;
# layers 2-3 come from the Steam cloud, so the catalog stays current even
# while the rig sleeps.
OWNED_MAX_AGE_S = 6 * 3600      # playtime/recency drift slowly; one call/6h
_sync_lock = threading.Lock()


def _iso_age(index, key):
    """Seconds since index[key] (iso timestamp), or None if absent/unparseable."""
    try:
        return time.time() - time.mktime(time.strptime(index[key],
                                                        "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        return None


def sync(meta_limit=200):
    """Full catalog refresh for the background thread: installed every call
    (cheap, and install state changes), owned when stale >6h, metadata top-up
    for any new appid. Steam layers are skipped without keys, never crash.
    Non-reentrant: a second caller while one runs is a no-op, so rapid
    session-boundary calls can't stack concurrent metadata crawls."""
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        # Layer 1b (collections) only when layer 1 SUCCEEDED - both need the PC
        # awake, so gating on refresh()'s result spares a sleeping sync a second
        # 15 s ssh timeout (and keeps the blind test offline: a mocked refresh
        # returns non-zero, so this never reaches ssh there).
        if refresh() == 0:                          # layer 1 (fail-softs asleep)
            refresh_collections()                   # layer 1b (PC-dependent too)
        # Layer 4 (deals) is keyless - GetWishlist/GetItems/featuredcategories
        # all are - so it runs BEFORE the key gate: a rig with only a steamId64
        # still gets "anything on my wishlist on sale". A handful of calls, not
        # a per-appid crawl, so it needs no pacing under the lock.
        d_age = _iso_age(load_deals(), "refreshed")
        if d_age is None or d_age > DEALS_MAX_AGE_S:
            refresh_deals()
        s = cglib.load_secrets()
        if not (cglib.real_key(s.get("steamApiKey"))
                and str(s.get("steamId64", "")).isdigit()):
            return                                  # no Steam key: layers 1+4 only
        age = _iso_age(load(), "ownedRefreshed")
        if age is None or age > OWNED_MAX_AGE_S:
            refresh_owned()                         # layer 2
        index = load()
        appids = {r["appid"] for r in index.get("installed", [])}
        appids.update(int(a) for a in index.get("owned", {}))
        if any(str(a) not in load_meta() for a in appids):
            refresh_meta(list(appids), meta_limit)  # layer 3 (top-up only)
    except Exception as e:
        log.error("sync_failed", err=str(e))
    finally:
        _sync_lock.release()


def query_terms(limit=30):
    """The words people use to ASK about games: distinct tags/genres across
    the catalog, frequency-ranked. Fed to Flux as extra keyterms - titles
    alone don't teach the STT this vocabulary, which is how a spoken "mech
    games" transcribed as "met games"."""
    counts = {}
    for m in load_meta().values():
        for term in (m.get("tags") or []) + (m.get("genres") or []):
            t = term.lower()
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts, key=lambda t: -counts[t])[:limit]


def catalog_lines():
    """Compact rows for the assistant's context - one line per game,
    installed first. appid|name|tags|genres|hours|lastPlayed|installed|ctrl"""
    index = load()
    meta = load_meta()
    owned = {k: v for k, v in index.get("owned", {}).items()
             if int(k) not in NOT_GAMES}
    installed_ids = {r["appid"] for r in index.get("installed", [])
                     if r["appid"] not in NOT_GAMES}
    rows = {r["appid"]: r["name"] for r in index.get("installed", [])
            if r["appid"] not in NOT_GAMES}
    for appid, o in owned.items():
        rows.setdefault(int(appid), o.get("name") or f"app {appid}")
    # Playtest/beta stubs have no metadata and pollute recommendations.
    rows = {a: n for a, n in rows.items() if not n.endswith(" Playtest")}
    lines = []
    for appid, name in rows.items():
        o = owned.get(str(appid), {})
        m = meta.get(str(appid), {})
        # Day precision, not month: with two games both reading "2026-08"
        # the model guessed at "what did I play last" and guessed wrong,
        # then had to disown the answer as month-only data (2026-08-15).
        # The store timestamp is exact; three more chars per row buy the
        # question back.
        last = (time.strftime("%Y-%m-%d", time.localtime(o["last"]))
                if o.get("last") else "never")
        lines.append((appid in installed_ids, o.get("hours", 0), (
            f"{appid}|{name}|{','.join(m.get('tags', [])[:5])}"
            f"|{','.join(m.get('genres', [])[:3])}|{o.get('hours', 0)}h"
            f"|{last}|{'inst' if appid in installed_ids else 'notinst'}"
            f"|{m.get('controller', '?')}")))
    lines.sort(key=lambda t: (not t[0], -t[1]))
    return [l for _, _, l in lines]


def catalog():
    lines = catalog_lines()
    text = "\n".join(lines)
    print(text)
    print(f"\n# {len(lines)} games, ~{len(text) // 4} tokens", file=sys.stderr)
    return 0


def probe(args):
    """Manual layer-4 smoke test - the one that confirms live endpoint shapes a
    keyless checkout cannot see. `python library.py probe deals|search <terms>|
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
    print("usage: library.py sync | refresh [--local-steam] [--owned] "
          "[--meta [N]] | show | catalog | probe <deals|search ...|reviews "
          "<appid>|news <appid>|hltb <name>|trending|recent>")
    return 2


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["sync"]:
        sync()
        sys.exit(0)
    elif args[:1] == ["refresh"]:
        rc = refresh(local="--local-steam" in args)
        if "--owned" in args:
            rc = refresh_owned() or rc
        if "--meta" in args:
            i = args.index("--meta")
            n = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else 200
            refresh_meta([r["appid"] for r in load().get("installed", [])], n)
        sys.exit(rc)
    elif args[:1] == ["show"]:
        sys.exit(show())
    elif args[:1] == ["catalog"]:
        sys.exit(catalog())
    elif args[:1] == ["probe"]:
        sys.exit(probe(args[1:]))
    else:
        sys.exit(usage())
