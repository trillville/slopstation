"""Tools that read Steam: the catalog, the store, and the account's lists."""

import concurrent.futures

from slopstation.agent.llm.registry import ToolContext, ToolSpec
from slopstation.agent.tools import library, steamstore

GET_GAME_DETAILS = """\
Details for one appid: tags/description/score from the catalog, plus any
live facets you ask for. Request facets only when the question needs them -
each is a live store call. 'price' = current price and discount; 'reviews'
= review score and a few recent comments (for 'what are people saying',
pass the DLC's own appid); 'news' = recent patch/update notes; 'hltb' = how
many hours to beat. Works for games the user does NOT own too. When the
question is about ONE named game's reviews, price, updates or length, this
is the answer and web search is not: Steam's own review score and patch
notes are better than a search result and arrive instantly."""

LIST_GAMES = """\
Read a ready-made list of games. source: 'wishlist_on_sale' (the user's
wishlist items currently discounted), 'specials' (today's featured store
sales), 'trending' (most-played right now), 'recently_played' (what the
user played in the last two weeks), 'downloading' (Steam's raw client
activity, including a finalizing phase - describe finalizing as finalizing,
never as a download, and use this source only when the user explicitly asks
for Steam's own client activity). Use this for 'anything on sale', 'what's
on my wishlist', 'what's popular', 'what have I been playing', 'how far
along is the Steam download'. General Slopstation operation status belongs
to list_operations. Leads with names and prices - not a research task."""

SEARCH_STORE = """\
Search the Steam store with filters and get back names + prices immediately
- this is the fast, factual path for 'find me a <kind of> game [under $N]
[on sale]'. Pass genres/features as tags (e.g. 'Roguelike', 'Co-op'), a
title fragment as term, a dollar cap as max_price. Use this when Steam's own
filters can answer."""

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
                "items": {
                    "type": "string",
                    "enum": ["price", "reviews", "news", "hltb"],
                },
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
        ),
    ),
    ToolSpec(
        "list_games",
        LIST_GAMES,
        {
            "source": {
                "type": "string",
                "enum": [
                    "wishlist_on_sale",
                    "specials",
                    "trending",
                    "recently_played",
                    "downloading",
                ],
            }
        },
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
            "steam download progress",
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
]


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
        # because they need the resolved game name.
        store_on = voice is None or voice.get("steamDataTools", True)
        want = (args.get("facets") or []) if store_on else []
        tasks = {}
        if "price" in want:
            tasks["price"] = lambda: steamstore.store_items([appid]).get(appid)
        if "reviews" in want:
            tasks["reviews"] = lambda: steamstore.fetch_reviews(appid)
        if "news" in want:
            tasks["news"] = lambda: steamstore.fetch_news(appid)
        facets = {}
        if tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                futs = {k: ex.submit(fn) for k, fn in tasks.items()}
                for k, f in futs.items():
                    try:
                        facets[k] = f.result()
                    except Exception as e:
                        log.warn("facet_failed", facet=k, appid=appid, err=str(e))
                        facets[k] = None
        if not name:
            name = (facets.get("price") or {}).get("name")
        if want and not name:
            # hltb needs a name, and nameless facet payloads let the model
            # misattribute results across titles.
            name = (steamstore.store_items([appid]).get(appid) or {}).get("name")
        if "hltb" in want and name:
            facets["hltb"] = steamstore.fetch_hltb(name)
        if not (meta or name or any(facets.values())):
            return {"ok": False, "error": "unknown appid"}
        return {
            "ok": True,
            "name": name,
            "installed": installed,
            **(meta or {}),
            **{k: v for k, v in facets.items() if v},
        }

    def list_games(args):
        """Sale/trending/recent lists. wishlist_on_sale and specials come from
        the precomputed state/deals.json (~0 ms); trending and recently_played
        are live calls. downloading goes over steam_session.py, which
        self-gates on its refresh token."""
        source = args.get("source")
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
        if source == "downloading":
            if steam is None or not steam.available():
                return {
                    "ok": False,
                    "error": "download status isn't set up - the "
                    "account session hasn't been enrolled",
                }
            try:
                return {"ok": True, "source": source, "games": steam.download_status()}
            except Exception as e:
                log.error("download_status_error", err=str(e))
                return {
                    "ok": False,
                    "error": "couldn't reach Steam for the download status just now",
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

    return {
        "get_game_details": get_game_details,
        "list_games": list_games,
        "search_store": search_store,
    }
