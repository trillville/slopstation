"""Fetch Steam deals, search results, reviews, news, and completion times.

Deals are cached in state/deals.json. Most requests use ``_get`` so network
failures return no data instead of stopping the agent.

CLI:
    python -m slopstation.agent.tools.steamstore <deals|search ...|reviews <appid>
                                                  |news <appid>|hltb <name>|trending|recent>
"""

from __future__ import annotations

import json
import re
import sys
import time

from slopstation import config, logbook, paths, statefile
from slopstation.agent.tools import library

log = logbook.logger("library")

STORE = "https://store.steampowered.com"
API = "https://api.steampowered.com"


# wishlist-on-sale + specials snapshot
def deals_file():
    return paths.state("deals.json")


DEALS_MAX_AGE_S = 6 * 3600  # prices move at sale boundaries


# per-game how-long-to-beat, {fuzzy_key: {main, extra, complete}}, forever
def hltb_cache_file():
    return paths.state("hltb-cache.json")


# {tag_name_lower: tagid}, weekly
def tagmap_file():
    return paths.state("store-tags.json")


TAGMAP_MAX_AGE_S = 7 * 24 * 3600


def _get(url: str, params: dict | None = None, timeout: float = 20):
    """Fetch JSON and return ``None`` on request or decoding errors."""
    import requests

    try:
        r = requests.get(
            url,
            params=params or {},
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:  # network, non-2xx, or non-JSON body
        log.warn("store_fetch_failed", url=url.rsplit("/", 1)[-1] or url, err=str(e))
        return None


def _cc() -> str:
    """Country code for prices, from voice.location.country (defaults US)."""
    try:
        return (
            config.current().get("voice", {}).get("location", {}).get("country") or "US"
        ).upper()
    except Exception:
        return "US"


def store_items(appids: list[int], cc: str | None = None) -> dict:
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
            "ids": [{"appid": a} for a in appids[i : i + 100]],
            "context": {"language": "english", "country_code": cc, "steam_realm": 1},
            "data_request": {"include_all_purchase_options": True},
        }
        d = _get(
            f"{API}/IStoreBrowseService/GetItems/v1/", {"input_json": json.dumps(body)}
        )
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


def fetch_wishlist_on_sale(steamid: str, cc: str | None = None) -> list[dict]:
    """Keyless GetWishlist -> GetItems -> the discounted ones, best first."""
    d = _get(f"{API}/IWishlistService/GetWishlist/v1/", {"steamid": steamid})
    items = ((d or {}).get("response", {}) or {}).get("items", []) or []
    # GetItems keys by int; a string appid would silently drop the game.
    appids = [int(it["appid"]) for it in items if it.get("appid")]
    priced = store_items(appids, cc)
    on_sale = [
        {"appid": a, **priced[a]}
        for a in appids
        if a in priced and priced[a]["discount"] > 0
    ]
    on_sale.sort(key=lambda g: -g["discount"])
    return on_sale


def fetch_specials(cc: str | None = None) -> list[dict]:
    """Front-page specials feed. Curated, ~a couple dozen, not exhaustive."""
    d = _get(f"{STORE}/api/featuredcategories", {"cc": cc or _cc(), "l": "english"})
    items = ((d or {}).get("specials", {}) or {}).get("items", []) or []
    out = []
    for it in items:
        if it.get("id") in library.NOT_GAMES:
            continue
        out.append(
            {
                "appid": it.get("id"),
                "name": library.ascii_only(it.get("name", "")),
                "discount": int(it.get("discount_percent", 0) or 0),
                "final": (it.get("final_price", 0) or 0) / 100 or None,
            }
        )
    return out


def fetch_trending(cc: str | None = None) -> list[dict]:
    """GetMostPlayedGames -> names via GetItems. Keyless, by concurrents."""
    d = _get(f"{API}/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response", {}) or {}).get("ranks", []) or []
    appids = [
        int(r["appid"]) for r in ranks[:20] if r.get("appid")
    ]  # int keys, see wishlist
    named = store_items(appids, cc)
    return [
        {"appid": a, "rank": i + 1, "name": named.get(a, {}).get("name") or f"app {a}"}
        for i, a in enumerate(appids)
    ]


def fetch_recently_played() -> list[dict]:
    """GetRecentlyPlayedGames. Own key only. Two weeks is all Steam offers."""
    creds = library.steam_creds()
    if not creds:
        return []
    d = _get(
        f"{API}/IPlayerService/GetRecentlyPlayedGames/v1/",
        {"key": creds[0], "steamid": creds[1]},
    )
    games = ((d or {}).get("response", {}) or {}).get("games", []) or []
    return [
        {
            "appid": g.get("appid"),
            "name": library.ascii_only(g.get("name", "")),
            "hours2w": round(g.get("playtime_2weeks", 0) / 60, 1),
        }
        for g in games
        if g.get("appid")
    ]


def _tag_map():
    """{tag_name_lower: tagid} for turning a spoken genre into a search filter.
    Cached weekly; GetTagList needs the key, else {} and search goes term-only."""
    try:
        fresh = time.time() - tagmap_file().stat().st_mtime < TAGMAP_MAX_AGE_S
    except OSError:  # missing or a stat race -> refetch
        fresh = False
    if fresh:
        cached = statefile.load(tagmap_file(), None)
        if cached is not None:
            return cached
    s = config.secrets()
    if not config.real_key(s.get("steamApiKey")):
        return {}
    d = _get(
        f"{API}/IStoreService/GetTagList/v1/",
        {"key": s["steamApiKey"], "language": "english"},
    )
    tags = ((d or {}).get("response", {}) or {}).get("tags", []) or []
    out = {
        library.ascii_only(t.get("name", "")).lower(): t.get("tagid")
        for t in tags
        if t.get("name") and t.get("tagid")
    }
    if out:
        statefile.write(tagmap_file(), out, indent=1)
    return out


def fetch_store_search(term="", tags=None, max_price=None, on_sale=False, cc=None):
    """Keyless /search/results for the appid list, then GetItems for names and
    prices. Tag names -> tagids via the cached map; unknown tags are dropped."""
    # fuzzy_key: 'Rogue-like' must find Steam's 'Roguelike'.
    tmap = {library.fuzzy_key(k): v for k, v in _tag_map().items()}
    tagids = [
        str(tmap[library.fuzzy_key(t)])
        for t in (tags or [])
        if library.fuzzy_key(t) in tmap
    ]
    params = {
        "term": term or "",
        "cc": cc or _cc(),
        "l": "english",
        "count": 50,
        "infinite": 1,
        "json": 1,
    }
    if tagids:
        params["tags"] = ",".join(tagids)
    if max_price:
        params["maxprice"] = int(max_price)
    if on_sale:
        params["specials"] = 1
    d = _get(f"{STORE}/search/results/", params)
    html = (d or {}).get("results_html", "") or ""
    seen, appids = set(), []
    for m in re.finditer(r'data-ds-appid="(\d+)"', html):  # capsule attr only
        a = int(m.group(1))
        if a not in seen and a not in library.NOT_GAMES:
            seen.add(a)
            appids.append(a)
        if len(appids) >= 20:
            break
    named = store_items(appids, cc)
    rows = [{"appid": a, **named[a]} for a in appids if a in named]
    if max_price:  # GetItems is truth on price; re-clip
        cap = int(max_price) * 100
        rows = [r for r in rows if not r.get("price") or r["price"] <= cap]
    return rows[:12]


def fetch_reviews(appid: int) -> dict | None:
    """/appreviews summary + recent snippets. For DLC, pass the DLC's appid."""
    d = _get(
        f"{STORE}/appreviews/{int(appid)}",
        {
            "json": 1,
            "language": "english",
            "filter": "recent",
            "num_per_page": 5,
            "purchase_type": "all",
        },
    )
    if not d or not d.get("query_summary"):
        return None
    q = d["query_summary"]
    snippets = [
        library.ascii_only(r.get("review", ""))[:280]
        for r in (d.get("reviews") or [])[:3]
        if r.get("review")
    ]
    return {
        "desc": q.get("review_score_desc"),
        "positive_pct": (
            round(100 * q.get("total_positive", 0) / q["total_reviews"])
            if q.get("total_reviews")
            else None
        ),
        "total": q.get("total_reviews"),
        "snippets": snippets,
    }


def fetch_news(appid: int, count: int = 3) -> list[dict]:
    """GetNewsForApp, patch notes preferred. Keyless, titles only."""
    params = {"appid": int(appid), "count": count, "maxlength": 1}
    for tags in ({"tags": "patchnotes"}, {}):  # no patch notes -> any announcement
        d = _get(f"{API}/ISteamNews/GetNewsForApp/v2/", {**params, **tags})
        items = ((d or {}).get("appnews", {}) or {}).get("newsitems", []) or []
        if items:
            break
    return [
        {
            "title": library.ascii_only(n.get("title", "")),
            "date": time.strftime("%Y-%m-%d", time.localtime(n.get("date", 0))),
        }
        for n in items[:count]
        if n.get("title")
    ]


def fetch_hltb(name: str) -> dict | None:
    """Fetch and cache completion times from HowLongToBeat."""
    key = library.fuzzy_key(name)
    cache = statefile.load(hltb_cache_file(), {})
    if key in cache:
        return cache[key]
    hltb = None
    try:
        from howlongtobeatpy import HowLongToBeat

        best = None
        for e in HowLongToBeat().search(name) or []:
            if best is None or (e.similarity or 0) > (best.similarity or 0):
                best = e
        if best:
            hltb = {
                "main": best.main_story,
                "extra": best.main_extra,
                "complete": best.completionist,
            }
    except Exception as e:  # missing pin, or endpoint churn
        log.warn("hltb_failed", name=name[:80], err=str(e))
        return None
    cache[key] = hltb
    statefile.write(hltb_cache_file(), cache, indent=1)
    return hltb


# --- the wider data lane (keyless, or the account's own key) ------------------


def _strip_html(text: str, limit: int = 400) -> str:
    text = re.sub(r"<br\s*/?>", " ", str(text or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return library.ascii_only(re.sub(r"\s+", " ", text)).strip()[:limit]


def fetch_appdetails(appid: int) -> dict | None:
    """The store's appdetails data block for one app, or None."""
    d = _get(
        f"{STORE}/api/appdetails", {"appids": int(appid), "cc": _cc(), "l": "english"}
    )
    entry = (d or {}).get(str(int(appid))) or {}
    return entry.get("data") if entry.get("success") else None


def fetch_dlc(appid: int, data: dict | None = None) -> list[dict]:
    """The DLC list with prices, from appdetails' ids and GetItems."""
    data = data if data is not None else fetch_appdetails(appid)
    ids = [int(a) for a in (data or {}).get("dlc", []) or []][:30]
    priced = store_items(ids)
    return [{"appid": a, **priced[a]} for a in ids if a in priced]


def fetch_requirements(appid: int, data: dict | None = None) -> dict | None:
    data = data if data is not None else fetch_appdetails(appid)
    req = (data or {}).get("pc_requirements") or {}
    if not isinstance(req, dict) or not req:
        return None
    return {
        k: _strip_html(v)
        for k, v in (
            ("minimum", req.get("minimum")),
            ("recommended", req.get("recommended")),
        )
        if v
    }


def fetch_release(appid: int, data: dict | None = None) -> dict | None:
    data = data if data is not None else fetch_appdetails(appid)
    if not data:
        return None
    rd = data.get("release_date") or {}
    return {
        "date": rd.get("date"),
        "coming_soon": bool(rd.get("coming_soon")),
        "developers": (data.get("developers") or [])[:3],
        "publishers": (data.get("publishers") or [])[:2],
    }


def fetch_players_now(appid: int) -> int | None:
    d = _get(
        f"{API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/", {"appid": int(appid)}
    )
    count = ((d or {}).get("response", {}) or {}).get("player_count")
    return int(count) if isinstance(count, int) else None


def fetch_achievements(appid: int) -> dict | None:
    """The account's progress in one game against the global unlock rates.
    Needs the API key; None without it or when the game has none."""
    creds = library.steam_creds()
    if not creds:
        return None
    key, steamid = creds
    schema = _get(
        f"{API}/ISteamUserStats/GetSchemaForGame/v2/",
        {"key": key, "appid": int(appid), "l": "english"},
    )
    defined = (
        ((schema or {}).get("game", {}) or {}).get("availableGameStats", {}) or {}
    ).get("achievements", []) or []
    if not defined:
        return None
    names = {a.get("name"): a for a in defined if a.get("name")}
    mine = _get(
        f"{API}/ISteamUserStats/GetPlayerAchievements/v1/",
        {"key": key, "steamid": steamid, "appid": int(appid)},
    )
    stats = (mine or {}).get("playerstats", {}) or {}
    if stats.get("success") is False:
        # An unowned game, or a private profile: no progress to report.
        return None
    got = stats.get("achievements", []) or []
    rates = _get(
        f"{API}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/",
        {"gameid": int(appid)},
    )
    pct = {
        a.get("name"): float(a.get("percent", 0) or 0)
        for a in ((rates or {}).get("achievementpercentages", {}) or {}).get(
            "achievements", []
        )
        or []
    }
    unlocked = [a for a in got if a.get("achieved")]
    missing = [a for a in got if not a.get("achieved")]

    def row(a):
        meta = names.get(a.get("apiname"), {})
        return {
            "name": library.ascii_only(meta.get("displayName") or a.get("apiname", "")),
            "desc": library.ascii_only(meta.get("description") or "")[:120],
            "global_pct": round(pct.get(a.get("apiname"), 0.0), 1),
            "unlocked": time.strftime(
                "%Y-%m-%d", time.localtime(a.get("unlocktime", 0))
            )
            if a.get("unlocktime")
            else None,
        }

    unlocked.sort(key=lambda a: -int(a.get("unlocktime", 0) or 0))
    missing.sort(key=lambda a: -pct.get(a.get("apiname"), 0.0))
    return {
        "total": len(defined),
        "unlocked": len(unlocked),
        "percent": round(100 * len(unlocked) / len(defined)) if defined else 0,
        "recent": [row(a) for a in unlocked[:5]],
        # Closest to done: the most commonly earned ones still missing.
        "next_up": [row(a) for a in missing[:5]],
        "rarest_held": [
            row(a)
            for a in sorted(unlocked, key=lambda a: pct.get(a.get("apiname"), 100))[:3]
        ],
    }


_PERSONA = {
    0: "offline",
    1: "online",
    2: "busy",
    3: "away",
    4: "snooze",
    5: "trading",
    6: "playing",
}


def fetch_friends() -> list[dict] | None:
    """Friends with their state and what they play, online first. Needs the
    key; friends whose profiles hide their status show as unknown."""
    creds = library.steam_creds()
    if not creds:
        return None
    key, steamid = creds
    d = _get(
        f"{API}/ISteamUser/GetFriendList/v1/",
        {"key": key, "steamid": steamid, "relationship": "friend"},
    )
    ids = [
        f.get("steamid")
        for f in ((d or {}).get("friendslist", {}) or {}).get("friends", []) or []
        if f.get("steamid")
    ]
    rows: list[dict] = []
    for i in range(0, len(ids), 100):
        s = _get(
            f"{API}/ISteamUser/GetPlayerSummaries/v2/",
            {"key": key, "steamids": ",".join(ids[i : i + 100])},
        )
        for p in ((s or {}).get("response", {}) or {}).get("players", []) or []:
            state = int(p.get("personastate", 0) or 0)
            # A private profile answers offline for everyone; say unknown.
            visible = int(p.get("communityvisibilitystate", 3) or 3) == 3
            rows.append(
                {
                    "name": library.ascii_only(p.get("personaname", "")),
                    "state": "playing"
                    if p.get("gameid")
                    else _PERSONA.get(state, "unknown")
                    if visible
                    else "unknown",
                    "playing": library.ascii_only(p.get("gameextrainfo", "")) or None,
                    "appid": int(p["gameid"])
                    if str(p.get("gameid", "")).isdigit()
                    else None,
                    "last_seen": time.strftime(
                        "%Y-%m-%d", time.localtime(p.get("lastlogoff", 0))
                    )
                    if p.get("lastlogoff")
                    else None,
                }
            )
    rows.sort(
        key=lambda r: (
            r["state"] == "offline",
            r["state"] != "playing",
            r["name"].lower(),
        )
    )
    return rows


FEATURED_SECTIONS = ("new_releases", "top_sellers", "coming_soon")


def fetch_featured(section: str, cc: str | None = None) -> list[dict] | None:
    """One of the store's front-page feeds: new_releases, top_sellers,
    coming_soon. Curated by Steam, a couple dozen each. None when the store
    did not answer, so an outage is never read as an empty feed."""
    if section not in FEATURED_SECTIONS:
        return []
    d = _get(f"{STORE}/api/featuredcategories", {"cc": cc or _cc(), "l": "english"})
    if d is None:
        return None
    items = (d.get(section, {}) or {}).get("items", []) or []
    out = []
    for it in items:
        if it.get("id") in library.NOT_GAMES:
            continue
        out.append(
            {
                "appid": it.get("id"),
                "name": library.ascii_only(it.get("name", "")),
                "discount": int(it.get("discount_percent", 0) or 0),
                "final": (it.get("final_price", 0) or 0) / 100 or None,
            }
        )
    return out


def fetch_wishlist(steamid: str, cc: str | None = None) -> list[dict] | None:
    """The whole wishlist with prices, in the user's own priority order. None
    when Steam did not answer."""
    d = _get(f"{API}/IWishlistService/GetWishlist/v1/", {"steamid": steamid})
    if d is None:
        return None
    items = (d.get("response", {}) or {}).get("items", []) or []
    items = [it for it in items if it.get("appid")]
    items.sort(key=lambda it: int(it.get("priority", 0) or 0))
    priced = store_items([int(it["appid"]) for it in items], cc)
    return [
        {
            "appid": int(it["appid"]),
            "priority": it.get("priority"),
            **priced.get(int(it["appid"]), {}),
        }
        for it in items
    ]


def load_deals() -> dict:
    return statefile.load(deals_file(), {})


def refresh_deals() -> int:
    """Precompute the feed answers into state/deals.json, read by list_games
    and the assistant. The wishlist half needs the key; specials is keyless."""
    s = config.secrets()
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
    statefile.write(deals_file(), deals, indent=1)
    log("deals_synced", specials=len(specials), wishlist=len(wishlist))
    return 0


def probe(args: list[str]) -> int:
    """Run a live store request from the command line."""
    what = args[0] if args else "deals"
    out: object
    if what == "deals":
        refresh_deals()
        out = load_deals()
    elif what == "search":
        out = fetch_store_search(term=" ".join(args[1:]))
    elif what == "reviews":
        out = fetch_reviews(int(args[1]))
    elif what == "news":
        out = fetch_news(int(args[1]))
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


def usage() -> int:
    print(
        "usage: python -m slopstation.agent.tools.steamstore <deals|search ...|reviews <appid>|news <appid>"
        "|hltb <name>|trending|recent>"
    )
    return 2


if __name__ == "__main__":
    sys.exit(probe(sys.argv[1:]))
