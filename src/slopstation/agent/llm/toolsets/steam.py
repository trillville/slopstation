"""Tools that read Steam: the catalog, the store, and the account's lists."""

import concurrent.futures
import time
from typing import Any

from slopstation import config
from slopstation.agent.llm.registry import ToolContext, ToolSpec
from slopstation.agent.tools import library, steamstore

GET_GAME_DETAILS = """\
Details for one appid: tags/description/score from the catalog, plus any
live facets you ask for. Request facets only when the question needs them -
each is a live store call. 'price' = current price and discount; 'reviews'
= review score and a few recent comments (for 'what are people saying',
pass the DLC's own appid); 'news' = recent patch/update notes; 'hltb' = how
many hours to beat; 'dlc' = its DLC with prices; 'achievements' = the
user's progress and what is closest to done; 'requirements' = PC specs;
'players_now' = how many are playing right now; 'release' = release date,
developer and publisher. Works for games the user does NOT own too. When
the question is about ONE named game's reviews, price, updates or length,
this is the answer and web search is not: Steam's own review score and
patch notes are better than a search result and arrive instantly."""

LIST_GAMES = """\
Read a ready-made list of games. source: 'wishlist_on_sale' (the user's
wishlist items currently discounted), 'wishlist' (the whole wishlist with
prices, in their priority order), 'specials' (today's featured store sales),
'trending' (most-played right now), 'recently_played' (what the user played
in the last two weeks), 'unplayed' (owned, never launched), 'most_played'
(owned, by lifetime hours), 'recently_updated' (installed games by their
last install or update). Use this for 'anything on sale', 'what's on my
wishlist', 'what's popular', 'what have I been playing', 'what have I never
played'. Steam's own download queue is download_status; Slopstation's
tracked work is list_operations. Leads with names - not a research task."""

SEARCH_STORE = """\
Search the Steam store with filters and get back names + prices immediately
- this is the fast, factual path for 'find me a <kind of> game [under $N]
[on sale]'. Pass genres/features as tags (e.g. 'Roguelike', 'Co-op'), a
title fragment as term, a dollar cap as max_price. Use this when Steam's own
filters can answer."""

SEARCH_LIBRARY = """\
Filter the user's own catalog when the inline list is too long to answer a
list question well: by tag or genre word, installed or not, controller
support, hours played, whether ever played; sorted by hours, last played,
name or last update. Returns the count and up to `limit` rows. For one named
game the catalog in the prompt is faster."""

MY_ACHIEVEMENTS = """\
The user's achievement progress in one game: unlocked out of total, the most
recent unlocks, the commonly earned ones still missing (closest to done), and
the rarest ones held, each with its global unlock rate. Owned games only."""

PLAYTIME = """\
Hours played across the library: lifetime or the last two weeks, most played
first, with the total. Returns the count and up to `limit` rows."""

FRIENDS = """\
The user's Steam friends: who is online or playing and what, then the rest
with when they were last seen. Returns the count and up to `limit` rows.
Friends whose profiles hide their status show as unknown."""

NEW_RELEASES = """\
The store's front-page feeds: 'new_releases', 'top_sellers' or 'coming_soon',
with prices and discounts. Curated by Steam, a couple dozen each. Returns the
count and up to `limit` rows."""

WISHLIST_EDIT = """\
Add a game to the user's wishlist or remove one, by store appid. Needs the
signed-in account session. Reports what Steam answered."""

FACETS = [
    "price",
    "reviews",
    "news",
    "hltb",
    "dlc",
    "achievements",
    "requirements",
    "players_now",
    "release",
]
SOURCES = [
    "wishlist_on_sale",
    "wishlist",
    "specials",
    "trending",
    "recently_played",
    "unplayed",
    "most_played",
    "recently_updated",
]
LIMIT_DEFAULT, LIMIT_MAX = 10, 40
LIMIT = {
    "type": "integer",
    "description": f"rows, default {LIMIT_DEFAULT}, at most {LIMIT_MAX}",
}

SPECS = [
    ToolSpec(
        "get_game_details",
        GET_GAME_DETAILS,
        {
            "appid": {
                "type": "integer",
                "description": "appid (catalog, or a store "
                "appid for a game the user doesn't own)",
            },
            "facets": {
                "type": "array",
                "items": {"type": "string", "enum": FACETS},
                "description": "which live facets to fetch; omit for catalog "
                "details only",
            },
        },
        ("appid",),
        risk="read",
        area="steam",
        keywords=(
            "price",
            "reviews",
            "patch notes",
            "how long to beat",
            "about this game",
            "details",
            "dlc",
            "system requirements",
            "player count",
            "release date",
        ),
    ),
    ToolSpec(
        "list_games",
        LIST_GAMES,
        {"source": {"type": "string", "enum": SOURCES}},
        ("source",),
        risk="read",
        area="steam",
        keywords=(
            "on sale",
            "wishlist",
            "specials",
            "trending",
            "popular",
            "recently played",
            "never played",
            "most played",
            "recently updated",
        ),
        needs=("steam_data",),
    ),
    ToolSpec(
        "search_store",
        SEARCH_STORE,
        {
            "term": {
                "type": "string",
                "description": "title words, or empty when "
                "searching purely by genre tags",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "genre/feature tag names, e.g. ['Roguelike','Co-op']",
            },
            "max_price": {"type": "integer", "description": "dollar price ceiling"},
            "on_sale": {"type": "boolean", "description": "restrict to discounted"},
        },
        (),
        risk="read",
        area="steam",
        keywords=("find a game", "search store", "genre", "under", "co-op", "buy"),
        needs=("steam_data",),
    ),
    ToolSpec(
        "search_library",
        SEARCH_LIBRARY,
        {
            "term": {"type": "string", "description": "a tag or genre word"},
            "installed": {"type": "boolean"},
            "controller": {"type": "string", "enum": ["full", "partial", "none"]},
            "played": {
                "type": "boolean",
                "description": "true: ever launched; false: never",
            },
            "min_hours": {"type": "number"},
            "max_hours": {"type": "number"},
            "sort": {
                "type": "string",
                "enum": ["hours", "last_played", "name", "updated"],
            },
            "limit": LIMIT,
        },
        (),
        risk="read",
        area="steam",
        keywords=(
            "my games with",
            "filter my library",
            "which of my games",
            "installed games",
            "controller support",
            "games i own that",
        ),
        default=False,
        paged=True,
    ),
    ToolSpec(
        "my_achievements",
        MY_ACHIEVEMENTS,
        {"appid": {"type": "integer", "description": "appid from the catalog"}},
        ("appid",),
        risk="read",
        area="steam",
        keywords=(
            "achievements",
            "how many achievements",
            "achievement progress",
            "rarest achievement",
            "trophies",
        ),
        default=False,
        needs=("steam_data",),
    ),
    ToolSpec(
        "playtime",
        PLAYTIME,
        {
            "period": {"type": "string", "enum": ["all", "two_weeks"]},
            "limit": LIMIT,
        },
        (),
        risk="read",
        area="steam",
        keywords=(
            "hours played",
            "playtime",
            "how much have i played",
            "total hours",
            "time in game",
        ),
        default=False,
    ),
    ToolSpec(
        "friends",
        FRIENDS,
        {"limit": LIMIT},
        (),
        risk="read",
        area="steam",
        keywords=(
            "friends online",
            "who is playing",
            "steam friends",
            "is anyone online",
            "friends list",
        ),
        default=False,
        needs=("steam_data",),
        paged=True,
    ),
    ToolSpec(
        "new_releases",
        NEW_RELEASES,
        {
            "section": {"type": "string", "enum": list(steamstore.FEATURED_SECTIONS)},
            "limit": LIMIT,
        },
        (),
        risk="read",
        area="steam",
        keywords=(
            "new releases",
            "top sellers",
            "coming soon",
            "what just came out",
            "upcoming games",
        ),
        default=False,
        needs=("steam_data",),
        paged=True,
    ),
    ToolSpec(
        "wishlist_edit",
        WISHLIST_EDIT,
        {
            "appid": {"type": "integer", "description": "store appid"},
            "action": {"type": "string", "enum": ["add", "remove"]},
        },
        ("appid", "action"),
        risk="act",
        area="steam",
        keywords=(
            "add to wishlist",
            "wishlist it",
            "remove from wishlist",
            "wishlist that game",
        ),
        default=False,
        needs=("steam_account",),
    ),
]


def _limit(args):
    try:
        n = int(args.get("limit") or LIMIT_DEFAULT)
    except (TypeError, ValueError):
        n = LIMIT_DEFAULT
    return max(1, min(n, LIMIT_MAX))


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None


def impls(ctx: ToolContext):
    log, steam, voice = ctx.log, ctx.steam, ctx.voice

    def get_game_details(args):
        appid = int(args.get("appid", 0))
        meta = library.load_meta().get(str(appid))
        name = library.installed_name(appid)
        installed = name is not None
        if not installed:
            o = library.load().get("owned", {}).get(str(appid))
            name = o.get("name") if o else None
        # Optional lookups run concurrently. Completion times run afterward
        # because they need the resolved game name; the appdetails facets
        # share one store call.
        store_on = voice is None or voice.get("steamDataTools", True)
        want = (args.get("facets") or []) if store_on else []
        tasks = {}
        if "price" in want:
            tasks["price"] = lambda: steamstore.store_items([appid]).get(appid)
        if "reviews" in want:
            tasks["reviews"] = lambda: steamstore.fetch_reviews(appid)
        if "news" in want:
            tasks["news"] = lambda: steamstore.fetch_news(appid)
        if "achievements" in want:
            tasks["achievements"] = lambda: steamstore.fetch_achievements(appid)
        if "players_now" in want:
            tasks["players_now"] = lambda: steamstore.fetch_players_now(appid)
        details = {"dlc", "requirements", "release"} & set(want)
        if details:

            def from_details():
                data = steamstore.fetch_appdetails(appid)
                out: dict[str, Any] = {}
                if "dlc" in details:
                    out["dlc"] = steamstore.fetch_dlc(appid, data)
                if "requirements" in details:
                    out["requirements"] = steamstore.fetch_requirements(appid, data)
                if "release" in details:
                    out["release"] = steamstore.fetch_release(appid, data)
                return out

            tasks["_details"] = from_details
        facets = {}
        if tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                futs = {k: ex.submit(fn) for k, fn in tasks.items()}
                for k, f in futs.items():
                    try:
                        value = f.result()
                    except Exception as e:
                        log.warn("facet_failed", facet=k, appid=appid, err=str(e))
                        value = None
                    if k == "_details" and isinstance(value, dict):
                        facets.update(value)
                    else:
                        facets[k] = value
        if not name:
            name = (facets.get("price") or {}).get("name")
        if want and not name:
            # hltb needs a name, and nameless facet payloads let the model
            # misattribute results across titles.
            name = (steamstore.store_items([appid]).get(appid) or {}).get("name")
        if "hltb" in want and name:
            facets["hltb"] = steamstore.fetch_hltb(name)
        if not (meta or name or any(v not in (None, [], {}) for v in facets.values())):
            return {"ok": False, "error": "unknown appid"}
        return {
            "ok": True,
            "name": name,
            "installed": installed,
            **(meta or {}),
            **{k: v for k, v in facets.items() if v not in (None, [], {})},
        }

    def _owned_rows():
        index = library.load()
        installed = {r["appid"]: r for r in index.get("installed", [])}
        rows = []
        for appid, o in index.get("owned", {}).items():
            appid = int(appid)
            if appid in library.NOT_GAMES or str(o.get("name", "")).endswith(
                " Playtest"
            ):
                continue
            rows.append(
                {
                    "appid": appid,
                    "name": o.get("name") or f"app {appid}",
                    "hours": float(o.get("hours", 0) or 0),
                    "hours2w": float(o.get("hours2w", 0) or 0),
                    "last_played": _day(o.get("last")),
                    "installed": appid in installed,
                    "updated": _day(installed.get(appid, {}).get("updated")),
                }
            )
        return rows

    def list_games(args):
        """Sale/trending/recent lists. wishlist_on_sale and specials come from
        the precomputed state/deals.json (~0 ms); trending, recently_played,
        wishlist are live calls; the owned sources read the catalog."""
        source = args.get("source")
        if source == "downloading":
            return {
                "ok": False,
                "error": "Steam's download status moved to the download_status tool",
            }
        if source == "wishlist_on_sale":
            rows = steamstore.load_deals().get("wishlist_on_sale")
            if rows is None:
                # Two causes, indistinguishable here: no steamId64
                # (refresh_deals never writes the key), or no sync yet.
                return {
                    "ok": False,
                    "error": "no wishlist data - either the "
                    "steamId64 isn't set, or the store sync hasn't run yet",
                }
            return {"ok": True, "source": source, "games": rows[:10]}
        if source == "wishlist":
            steamid = str(config.secrets().get("steamId64", ""))
            if not steamid.isdigit():
                return {
                    "ok": False,
                    "error": "steamId64 isn't set, so the wishlist can't be read",
                }
            rows = steamstore.fetch_wishlist(steamid)
            return {
                "ok": True,
                "source": source,
                "count": len(rows),
                "games": rows[:LIMIT_MAX],
            }
        if source == "specials":
            return {
                "ok": True,
                "source": source,
                "games": steamstore.load_deals().get("specials", [])[:10],
            }
        if source == "trending":
            return {
                "ok": True,
                "source": source,
                "games": steamstore.fetch_trending()[:10],
            }
        if source == "recently_played":
            rows = steamstore.fetch_recently_played()
            return {"ok": True, "source": source, "games": rows[:10]}
        if source in ("unplayed", "most_played", "recently_updated"):
            rows = _owned_rows()
            if source == "unplayed":
                rows = [r for r in rows if r["hours"] == 0]
                rows.sort(key=lambda r: (not r["installed"], r["name"].lower()))
            elif source == "most_played":
                rows.sort(key=lambda r: -r["hours"])
            else:
                rows = [r for r in rows if r["updated"]]
                rows.sort(key=lambda r: r["updated"] or "", reverse=True)
            return {
                "ok": True,
                "source": source,
                "count": len(rows),
                "games": rows[:10],
            }
        return {"ok": False, "error": f"unknown source {source}"}

    def search_store(args):
        """Steam's own filtered search. Tag names come from the caller (spoken
        genres); unknown ones are dropped, term still applies."""
        term = str(args.get("term", "")).strip()
        tags = args.get("tags") or []
        if not term and not tags:
            return {"ok": False, "error": "search needs a term or a genre tag"}
        rows = steamstore.fetch_store_search(
            term=term,
            tags=tags,
            max_price=args.get("max_price"),
            on_sale=bool(args.get("on_sale")),
        )
        return {"ok": True, "count": len(rows), "games": rows}

    def search_library(args):
        meta = library.load_meta()
        rows = _owned_rows()
        term = str(args.get("term") or "").lower().strip()
        if term:
            rows = [
                r
                for r in rows
                if any(
                    term in str(t).lower()
                    for t in (meta.get(str(r["appid"]), {}).get("tags") or [])
                    + (meta.get(str(r["appid"]), {}).get("genres") or [])
                )
                or term in r["name"].lower()
            ]
        if args.get("installed") is not None:
            rows = [r for r in rows if r["installed"] == bool(args["installed"])]
        if args.get("controller"):
            rows = [
                r
                for r in rows
                if meta.get(str(r["appid"]), {}).get("controller") == args["controller"]
            ]
        if args.get("played") is not None:
            rows = [r for r in rows if (r["hours"] > 0) == bool(args["played"])]
        try:
            if args.get("min_hours") is not None:
                rows = [r for r in rows if r["hours"] >= float(args["min_hours"])]
            if args.get("max_hours") is not None:
                rows = [r for r in rows if r["hours"] <= float(args["max_hours"])]
        except (TypeError, ValueError):
            return {"ok": False, "error": "hours must be numbers"}
        sort = str(args.get("sort") or "hours")
        key = {
            "hours": lambda r: -r["hours"],
            "last_played": lambda r: r["last_played"] or "",
            "name": lambda r: r["name"].lower(),
            "updated": lambda r: r["updated"] or "",
        }.get(sort)
        if key is None:
            return {
                "ok": False,
                "error": "sort must be hours, last_played, name or updated",
            }
        rows.sort(key=key, reverse=sort in ("last_played", "updated"))
        for r in rows:
            r["tags"] = (meta.get(str(r["appid"]), {}).get("tags") or [])[:4]
        limit = _limit(args)
        return {"ok": True, "count": len(rows), "games": rows[:limit]}

    def my_achievements(args):
        appid = int(args.get("appid", 0))
        if str(appid) not in library.load().get("owned", {}):
            return {"ok": False, "error": "achievements are read for owned games only"}
        out = steamstore.fetch_achievements(appid)
        if out is None:
            return {
                "ok": False,
                "error": "no achievement data - the game has none, or the steamApiKey is missing",
            }
        return {
            "ok": True,
            "appid": appid,
            "name": library.installed_name(appid),
            **out,
        }

    def playtime(args):
        period = str(args.get("period") or "all")
        if period not in ("all", "two_weeks"):
            return {"ok": False, "error": "period must be all or two_weeks"}
        rows = _owned_rows()
        key = "hours2w" if period == "two_weeks" else "hours"
        rows = [r for r in rows if r[key] > 0]
        rows.sort(key=lambda r: -r[key])
        limit = _limit(args)
        return {
            "ok": True,
            "period": period,
            "total_hours": round(sum(r[key] for r in rows), 1),
            "count": len(rows),
            "games": [
                {
                    "appid": r["appid"],
                    "name": r["name"],
                    "hours": round(r[key], 1),
                    "last_played": r["last_played"],
                }
                for r in rows[:limit]
            ],
        }

    def friends(args):
        rows = steamstore.fetch_friends()
        if rows is None:
            return {
                "ok": False,
                "error": "the steamApiKey isn't set, so friends can't be read",
            }
        online = [r for r in rows if r["state"] not in ("offline", "unknown")]
        limit = _limit(args)
        return {
            "ok": True,
            "count": len(rows),
            "online": len(online),
            "friends": rows[:limit],
        }

    def new_releases(args):
        section = str(args.get("section") or "new_releases")
        if section not in steamstore.FEATURED_SECTIONS:
            return {
                "ok": False,
                "error": f"section must be one of {', '.join(steamstore.FEATURED_SECTIONS)}",
            }
        rows = steamstore.fetch_featured(section)
        limit = _limit(args)
        return {
            "ok": True,
            "section": section,
            "count": len(rows),
            "games": rows[:limit],
        }

    def wishlist_edit(args):
        action = str(args.get("action") or "")
        if action not in ("add", "remove"):
            return {"ok": False, "error": "action must be add or remove"}
        try:
            appid = int(args.get("appid", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "appid must be an integer"}
        if appid <= 0:
            return {"ok": False, "error": "appid must be positive"}
        if steam is None or not steam.available():
            return {"ok": False, "error": "the Steam account session isn't enrolled"}
        if ctx.dispatch.dry_run:
            log("dry_run_would", action=f"wishlist {action} {appid}")
            return {"ok": True, "dry_run": True, "detail": f"would {action} {appid}"}
        method = "AddToWishlist" if action == "add" else "RemoveFromWishlist"
        try:
            _, eresult = steam._post(
                f"IWishlistService/{method}/v1",
                {"access_token": steam.access_token(), "appid": appid},
            )
        except Exception as e:
            log.error("wishlist_edit_error", appid=appid, err=str(e))
            return {"ok": False, "error": "couldn't reach Steam to change the wishlist"}
        if eresult not in (None, "1"):
            return {
                "ok": False,
                "error": f"Steam refused the wishlist change (code {eresult})",
            }
        return {"ok": True, "appid": appid, "action": action}

    return {
        "get_game_details": get_game_details,
        "list_games": list_games,
        "search_store": search_store,
        "search_library": search_library,
        "my_achievements": my_achievements,
        "playtime": playtime,
        "friends": friends,
        "new_releases": new_releases,
        "wishlist_edit": wishlist_edit,
    }
