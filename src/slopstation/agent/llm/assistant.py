"""Define the assistant prompt, tools, and tool implementations."""

import concurrent.futures
import json
import time
from typing import Any

from slopstation import sessionlock
from slopstation.agent.llm import media_tools, prompts

# tool spans; the module self-gates: REPL/bench are no-ops
from slopstation.agent.telemetry import sentry
from slopstation.agent.tools import library, steamstore


def system_instruction(cfg, interface="voice"):
    """Build the system prompt from configuration and the game catalog."""
    voice = cfg["voice"]
    inputs = voice.get("inputs", {})
    # A day with no zone resolves toward UTC and dates an evening brief
    # tomorrow. Empty timezone is a normal deployment.
    tz = voice.get("location", {}).get("timezone")
    # The clock too: without it the model has searched the web for the time.
    tail = [
        f"It is {time.strftime('%H:%M')} on {time.strftime('%Y-%m-%d')}"
        + (f" in {tz}." if tz else " local time.")
    ]
    if inputs:
        gaming = next(
            (k for k, v in inputs.items() if v == cfg.get("tvGamingCmd")), None
        )
        tail.append(
            f"TV inputs: {', '.join(inputs)}"
            + (f"; '{gaming}' starts a session if none is running." if gaming else ".")
        )
    tail.append(
        f"Volume runs 0-{voice['volumeMax']}, higher requests are clamped - "
        "confirm the level the tool actually returns. Mute is a blind toggle "
        "with no readable state - say you toggled it, never claim on or off."
    )
    if voice["assistantWebSearch"]:
        tail.append(prompts.WEB_SEARCH_RULE)
    style = prompts.TEXT_STYLE if interface == "text" else prompts.VOICE_STYLE
    input_rule = "" if interface == "text" else "\n\n" + prompts.VOICE_INPUT_RULE
    return (
        style + input_rule + "\n\n" + prompts.RULES + " " + " ".join(tail) + "\n\n"
        "CATALOG (appid|name|tags|genres|hours|lastPlayed YYYY-MM-DD or "
        "never|inst[:YYYY-MM-DD last install or update]/notinst|controller "
        "full/partial/none/?):\n" + "\n".join(library.catalog_lines())
    )


def known_appids():
    index = library.load()
    ids = {r["appid"] for r in index.get("installed", [])}
    ids.update(int(a) for a in index.get("owned", {}))
    return ids


def tool_impls(
    dispatch,
    log,
    operations=None,
    on_stop_listening=None,
    voice=None,
    steam=None,
    media=None,
):
    """Return the tool implementations enabled by the supplied services."""

    def _unknown(tool, appid):
        """The refusal for an appid outside the catalog, else None."""
        if appid in known_appids():
            return None
        log.warn("tool_refused", tool=tool, reason="unknown_appid", appid=appid)
        return {"ok": False, "error": f"appid {appid} is not in the catalog"}

    def launch_game(args):
        appid = int(args.get("appid", 0))
        if refused := _unknown("launch_game", appid):
            return refused
        if library.installed_name(appid) is None:
            return {
                "ok": False,
                "error": "that game is owned but not "
                "installed - installing needs the controller",
            }
        r = dispatch.play_game(appid)
        return {"ok": r.ok, "detail": r.detail}

    def quit_game(args):
        appid = int(args.get("appid", 0))
        if refused := _unknown("quit_game", appid):
            return refused
        r = dispatch.quit_game(appid)
        return {"ok": r.ok, "detail": r.detail}

    def install_game(args):
        """Get an owned-but-not-installed game downloading. Two paths, in
        order: the account session queues it silently when that lane is
        enrolled AND minting; otherwise put the game's Big Picture page on the
        TV to press Install. The fallback needs no token, but a live session."""
        appid = int(args.get("appid", 0))
        if refused := _unknown("install_game", appid):
            return refused
        if library.installed_name(appid) is not None:
            return {"ok": False, "error": "that game is already installed"}
        if dispatch.dry_run:
            detail = f"would start the download for appid {appid}"
            log("dry_run_would", action=detail)
            return {"ok": True, "dry_run": True, "detail": detail}
        if steam is not None and steam.available():
            try:
                r = steam.install(appid)
                if r.get("ok"):
                    if operations is not None:
                        owned = library.load().get("owned", {}).get(str(appid), {})
                        title = owned.get("name") or f"app {appid}"
                        try:
                            operation = operations.track_steam_install(
                                appid,
                                title,
                                turn=dispatch.utterance.turn,
                                verified=bool(r.get("verified")),
                            )
                            return {**r, "operation_id": operation["id"]}
                        except Exception as e:
                            # Submission already happened; tracking must not
                            # turn a successful external action into a refusal.
                            log.error("operation_track_failed", appid=appid, err=str(e))
                    return r
                log.warn("install_fallback", appid=appid, why=r.get("error"))
            except Exception as e:
                # available() proves the token is PRESENT, not that it still
                # mints (a web-audience token never does). Fall
                # through to the path that needs no credential.
                log.error("install_error", appid=appid, err=str(e))
        r = dispatch.nav("details", appid)
        if r.ok:
            return {
                "ok": True,
                "detail": "it's on the TV now - press Install and the download starts",
            }
        return {"ok": False, "error": r.detail}

    def nav(args):
        """Big Picture navigation. downloads/library/store need no appid;
        game_page needs an OWNED one, store_page any."""
        target = args.get("target")
        appid = args.get("appid")
        if target == "game_page":
            # The LIBRARY page - only an owned game has one.
            appid = int(appid or 0)
            if refused := _unknown("nav", appid):
                return refused
            r = dispatch.nav("details", appid)
        elif target == "store_page":
            # No catalog check: a store page is for a game they do NOT own.
            appid = int(appid or 0)
            if appid <= 0:
                return {"ok": False, "error": "I need the game's store appid"}
            r = dispatch.nav("store", appid)
        elif target == "collection":
            # Grammar mishears land here: resolve fuzzily, and on
            # a miss hand back the real names for the model to act on.
            rows = library.load().get("collections", [])
            if not rows:
                return {
                    "ok": False,
                    "error": "no collections are synced yet - "
                    "the PC has to be awake for that",
                }
            cid = None
            want = str(args.get("collection") or "").strip()
            if want:
                from slopstation.agent.tools import titles

                resolve = titles.build_collection_resolver(
                    (voice or {}).get("fuzzyTitleThreshold", 87)
                )
                cid, _ = resolve(want) if resolve else (None, None)
            if cid is None:
                return {
                    "ok": False,
                    "error": f"no collection matches {want!r}"
                    if want
                    else "which collection?",
                    "collections": [r["name"] for r in rows],
                }
            r = dispatch.nav("collection", cid)
        elif target in ("downloads", "library", "store"):
            r = dispatch.nav(target)
        else:
            return {"ok": False, "error": f"unknown nav target {target}"}
        return {"ok": r.ok, "detail": r.detail}

    plain = {
        "end_session": dispatch.end_session,
        "start_session": dispatch.start_session,
        "volume_up": dispatch.volume_up,
        "volume_down": dispatch.volume_down,
        "mute": dispatch.mute_toggle,
    }

    def control(args):
        action = args.get("action")
        if action == "set_volume":
            if "level" not in args:
                return {"ok": False, "error": "set_volume needs level 0-100"}
            r = dispatch.volume_set(int(args["level"]))
        elif action == "switch_input":
            r = dispatch.switch_input(str(args.get("input", "")))
        elif action in plain:
            r = plain[action]()
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        return {"ok": r.ok, "detail": r.detail}

    def stop_listening(args):
        """Acts on the CONVERSATION rather than the room. Not dry-run gated,
        unlike everything in dispatch.py: closing our own mic changes nothing
        on the TV or the PC."""
        if on_stop_listening is None:
            return {
                "ok": False,
                "error": "there is no open voice session to "
                "close - nothing is listening in the first place",
            }
        on_stop_listening()
        # end_turn: no second model turn, so nothing is spoken after this.
        return {
            "ok": True,
            "detail": "the mic is closed - the wake word is what reopens it",
            "end_turn": True,
        }

    def get_now_playing(args):
        # The PC reports RunningAppID 0 all through a launch, which reads as
        # "nothing is playing" while start_session says "already starting".
        # session_active is the same predicate that refusal uses
        # (sessionlock.active), so the two cannot disagree.
        active = sessionlock.active()
        launching = dispatch.launch_in_flight()
        r = dispatch.now_playing()
        if not r.ok:
            # Mid-launch the PC can be unreachable; the lock still answers.
            return {
                "ok": False,
                "error": r.detail,
                "session_active": active,
                "launching": launching,
            }
        appid = int(r.detail) if str(r.detail).isdigit() else 0
        return {
            "ok": True,
            "appid": appid,
            "name": library.installed_name(appid) if appid else None,
            "session_active": active,
            "launching": launching,
        }

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

    def list_operations(args):
        scope = args.get("scope", "active")
        if scope not in ("active", "recent"):
            return {"ok": False, "error": f"unknown operation scope {scope}"}
        return {
            "ok": True,
            "scope": scope,
            "operations": operations.for_assistant(
                scope, acknowledge=(scope == "recent" and not dispatch.dry_run)
            ),
        }

    impls = {
        "launch_game": launch_game,
        "control": control,
        "stop_listening": stop_listening,
        "get_now_playing": get_now_playing,
        "get_game_details": get_game_details,
        "list_games": list_games,
        "search_store": search_store,
        "quit_game": quit_game,
        "nav": nav,
        "install_game": install_game,
    }
    if operations is not None:
        impls["list_operations"] = list_operations
    if media is not None:
        impls.update(media_tools.tool_impls(dispatch, log, operations, media))
    if voice is not None and not voice.get("steamDataTools", True):
        for gated in ("list_games", "search_store"):
            impls.pop(gated, None)
    # install_game is always offered: without the account session it falls
    # back to putting the game's page on the TV, so it always does something.
    return impls


# name, description, JSON-schema properties, required; rendered per provider
# below. The description is the whole interface (prompts.py).
TOOL_DEFS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "launch_game",
        prompts.LAUNCH_GAME,
        {"appid": {"type": "integer", "description": "appid from the catalog"}},
        ["appid"],
    ),
    (
        "control",
        prompts.CONTROL,
        {
            "action": {
                "type": "string",
                "enum": [
                    "end_session",
                    "start_session",
                    "volume_up",
                    "volume_down",
                    "mute",
                    "set_volume",
                    "switch_input",
                ],
            },
            "level": {"type": "integer", "description": "volume level for set_volume"},
            "input": {
                "type": "string",
                "description": "spoken input name for switch_input; valid "
                "names are in the system prompt",
            },
        },
        ["action"],
    ),
    ("stop_listening", prompts.STOP_LISTENING, {}, []),
    ("get_now_playing", prompts.GET_NOW_PLAYING, {}, []),
    (
        "get_game_details",
        prompts.GET_GAME_DETAILS,
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
        ["appid"],
    ),
    (
        "list_games",
        prompts.LIST_GAMES,
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
        ["source"],
    ),
    (
        "search_store",
        prompts.SEARCH_STORE,
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
        [],
    ),
    (
        "quit_game",
        prompts.QUIT_GAME,
        {"appid": {"type": "integer", "description": "appid of the running game"}},
        ["appid"],
    ),
    (
        "nav",
        prompts.NAV,
        {
            "target": {
                "type": "string",
                "enum": [
                    "downloads",
                    "library",
                    "store",
                    "game_page",
                    "store_page",
                    "collection",
                ],
            },
            "appid": {
                "type": "integer",
                "description": "required for game_page (must "
                "be owned) and store_page (any Steam appid)",
            },
            "collection": {
                "type": "string",
                "description": "collection name, for target=collection",
            },
        },
        ["target"],
    ),
    (
        "install_game",
        prompts.INSTALL_GAME,
        {
            "appid": {
                "type": "integer",
                "description": "appid of an owned, not-yet-installed game",
            }
        },
        ["appid"],
    ),
    (
        "list_operations",
        prompts.LIST_OPERATIONS,
        {"scope": {"type": "string", "enum": ["active", "recent"]}},
        [],
    ),
    *media_tools.TOOL_DEFS,
]


def record_tool_call(name, args, out, log=None):
    """Record a tool call in Sentry and the local log."""
    try:
        sentry.tool_span(name, json.dumps(args)[:2000], json.dumps(out)[:2000])
    except Exception:
        pass
    if log:
        ok = out.get("ok") if isinstance(out, dict) else None
        log("tool_call", tool=name, ok=ok, args=json.dumps(args)[:300])


def function_schemas(impls, log):
    """Build Pipecat schemas that run blocking tools in worker threads."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

    def wrap(name, fn):
        async def handler(params):
            args = dict(params.arguments)
            try:
                out = await asyncio.to_thread(fn, args)
            except Exception as e:
                # Always return a result so a tool exception cannot hang the turn.
                log.error("tool_error", tool=name, err=repr(e))
                out = {
                    "ok": False,
                    "error": "that didn't go through - something upstream failed",
                }
            # After the call, so the span carries the RESULT. The await above
            # does not lose the OTel context (contextvars are per-task), so
            # this still parents onto Pipecat's llm span.
            record_tool_call(name, args, out, log)
            acknowledgment = (
                out.get("acknowledgment") if isinstance(out, dict) else None
            )
            end_turn = isinstance(out, dict) and bool(out.get("end_turn"))
            if end_turn:
                # The session is ending on this call (stop_listening): a
                # second model turn would only be a goodbye spoken to a
                # closing mic.
                await params.result_callback(
                    out, properties=FunctionCallResultProperties(run_llm=False)
                )
            elif acknowledgment:

                async def speak():
                    await params.pipeline_worker.queue_frame(
                        TTSSpeakFrame(str(acknowledgment))
                    )

                properties = FunctionCallResultProperties(
                    run_llm=False, on_context_updated=speak
                )
                await params.result_callback(out, properties=properties)
            else:
                await params.result_callback(out)

        return handler

    # Render only tools present in `impls` - the schema half of tool_impls'
    # gating.
    return [
        FunctionSchema(
            name=n, description=d, properties=p, required=r, handler=wrap(n, impls[n])
        )
        for n, d, p, r in TOOL_DEFS
        if n in impls
    ]


# `names` filters to the tools present in a given impls set, so a renderer
# can't offer a tool that isn't callable; None renders every TOOL_DEF.
def anthropic_tools(names=None):
    return [
        {
            "name": n,
            "description": d,
            "input_schema": {"type": "object", "properties": p, "required": r},
        }
        for n, d, p, r in TOOL_DEFS
        if names is None or n in names
    ]


def openai_tools(names=None):
    # Responses API tool shape is FLAT (name/parameters at top level) - the
    # nested {"function": {...}} form is chat-completions only.
    return [
        {
            "type": "function",
            "name": n,
            "description": d,
            "parameters": {"type": "object", "properties": p, "required": r},
        }
        for n, d, p, r in TOOL_DEFS
        if names is None or n in names
    ]


def _user_location(voice):
    """Non-empty location fields -> the 'approximate' user_location dict, or
    None when nothing is set. Both providers accept the identical shape."""
    loc = {k: v for k, v in voice["location"].items() if v}
    return {"type": "approximate", **loc} if loc else None


def server_tools(voice, provider):
    """Provider-native tools (the provider executes them; nothing in
    tool_impls), appended next to the TOOL_DEFS renders. Today: web search
    behind config.assistantWebSearch. Anthropic caps calls via max_uses;
    OpenAI has no cap knob, hence search_context_size low."""
    if not voice["assistantWebSearch"]:
        return []
    if provider == "openai":
        tool = {"type": "web_search", "search_context_size": "low"}
    else:
        tool = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": voice["assistantSearchMaxUses"],
        }
    loc = _user_location(voice)
    if loc:
        tool["user_location"] = loc
    return [tool]


# Both keys stay populated in config so flipping assistantProvider is the
# whole A/B, and neither lane hides behind a default.
MODEL_KEY = {"anthropic": "assistantModelAnthropic", "openai": "assistantModelOpenai"}
PROVIDER_KEY = {"anthropic": "anthropicApiKey", "openai": "openaiApiKey"}


def default_model(voice, provider):
    return voice[MODEL_KEY[provider]]
