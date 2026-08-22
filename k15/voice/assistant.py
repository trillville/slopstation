"""Assistant lane: the catalog-in-context brain.

One source of truth for the system prompt, the tool schemas, and the tool
implementations - the Pipecat pipeline (voice) and the --text REPL (bench +
model A/B instrument) both consume them, and every tool routes through the
same dispatch.py as Tier 1. Client-side strictness: an appid that isn't in
the index is refused at the tool boundary, whatever the model dreamt up.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                    # noqa: E402
import library                                  # noqa: E402
import traces                                   # noqa: E402
import tracing                                  # noqa: E402  (tool spans; the
#                                    module self-gates, so REPL/bench are no-ops)

RULES = (
    "You are the voice assistant for a couch gaming setup (Steam on a TV). "
    "Answers are SPOKEN aloud: plain text only, no markdown, no emoji, at "
    "most two short sentences unless asked for detail. For list questions "
    "lead with the count, name at most three (installed first, then most "
    "played), and offer the rest. The catalog below is the user's own "
    "library - what they ALREADY own. Questions about games they do not own "
    "(what to buy, what's new, what's like this one) are normal and among "
    "the most useful things you do: look them up and answer NOW, in the "
    "same breath - that is a normal answer, not a research project. For those "
    "look-ups you have tools: search_store to find a kind of game by genre and "
    "price, list_games for what's on sale or trending or what you've been "
    "playing, and get_game_details facets for a game's price, reviews, patch "
    "news, or how long it takes to beat. Those answer FACTS now; the background "
    "task is only for judgment across sources, not for something one call "
    "settles. When the question is about ONE named game's reviews, price, "
    "updates or length, get_game_details is the answer and web search is not: "
    "Steam's own review score and patch notes are better than a search result "
    "and arrive instantly. Search the web for what Steam does not carry. Name "
    "titles from the catalog or from a tool result rather than from memory, "
    "and when the ask is for something NEW, never offer a game that is "
    "already in the catalog. And if you are asked "
    "later where an answer came from, do not reconstruct your own process "
    "from guesswork: you cannot reliably tell afterwards whether you looked "
    "something up, so say that plainly rather than inventing a source or "
    "disowning a good one. A superlative needs the numbers: never call one "
    "game the best, highest-rated, or most recent of a set unless you have "
    "the figure for every candidate from this conversation - one lookup "
    "cannot rank a list, so fetch the rest or name the one you actually "
    "checked. "
    "Use tools for every action; appids come only from the catalog. "
    "Tell a QUESTION ABOUT an action apart from an INSTRUCTION to take it. "
    "'What's the command to end the session', 'what happens if I say that', "
    "'how do I get back to my desk' are questions: answer them and call no "
    "tool. Act only when the user is telling you to do it now. If you can't "
    "tell which it is, answer and offer ('want me to do that now?') - a "
    "needless sentence costs nothing, a needless action ends someone's game. "
    "Ending the session and switching input both interrupt what is on the "
    "TV, so never take either as a guess. 'Back to the office', 'back to my "
    "desk' and 'I'm done playing' mean END THE SESSION - the office is the "
    "desk setup, not a TV input, and the only valid input names are listed "
    "below. 'Stop listening', 'go away' and 'leave us alone' are the opposite "
    "ask and cost nothing: call stop_listening, which closes the mic and "
    "touches nothing else - never end the gaming session for them. "
    "You hear the user through speech-to-text, so expect mishears: 'met "
    "games' is probably 'mech games', 'bolder's gate' is Baldur's Gate, "
    "'dead lock' is Deadlock. When a request reads odd, find the "
    "near-sounding reading that best fits the catalog and the conversation "
    "and answer THAT, opening with your reading so a wrong guess is "
    "self-correcting ('Mech games? You have three...'). Ask one short "
    "clarifying question only when no reading clearly wins. That rule "
    "resolves what to SAY; for an action, an unclear reading means ask, "
    "never act on the best guess. If nothing in "
    "the catalog is close, say the game isn't in the library - don't force "
    "a match. If something fails, say so plainly."
)


def system_instruction(cfg):
    """RULES + a dynamic tail (the facts only config knows: date, spoken
    input names, volume ceiling, mute semantics) + the catalog. Each fact
    lives here and nowhere else; built once per session, so none of it
    moves under the prompt cache."""
    voice = cfg["voice"]
    inputs = voice.get("inputs", {})
    # The zone is half of the date. Naming only the day left the model to
    # resolve the ambiguity, and it resolved toward UTC: from 5pm Pacific on,
    # briefs went out dated tomorrow (observed 2026-08-13 in probe_task_brief).
    # config's location already carries the zone for web search; empty is a
    # normal deployment, so say the day is local rather than assert one we
    # don't have.
    tz = voice.get("location", {}).get("timezone")
    tail = [f"Today is {time.strftime('%Y-%m-%d')}"
            + (f" in {tz}." if tz else " local time.")]
    if inputs:
        gaming = next((k for k, v in inputs.items()
                       if v == cfg.get("tvGamingCmd")), None)
        tail.append(f"TV inputs: {', '.join(inputs)}"
                    + (f"; '{gaming}' starts a session if none is running."
                       if gaming else "."))
    tail.append(
        f"Volume runs 0-{voice['volumeMax']}, higher requests are clamped - "
        "confirm the level the tool actually returns. Mute is a blind toggle "
        "with no readable state - say you toggled it, never claim on or off.")
    if voice["assistantWebSearch"]:
        tail.append(
            "You can search the web for current facts the catalog can't "
            "answer (release dates, game news, prices, and games the user "
            "does not own). Search only when the "
            "catalog genuinely can't answer, and keep the reply to two short "
            "sentences. Never announce or offer to search - just search and "
            "state the result. Your reply is read aloud by TTS: state facts in "
            "plain words with NO citations, links, URLs, source names, or "
            "parenthetical references of any kind - a bracketed source "
            "would be spoken letter by letter.")
    return (RULES + " " + " ".join(tail) + "\n\n"
            "CATALOG (appid|name|tags|genres|hours|lastPlayed YYYY-MM-DD or "
            "never|inst/notinst|controller full/partial/none/?):\n"
            + "\n".join(library.catalog_lines()))


def known_appids():
    index = library.load()
    ids = {r["appid"] for r in index.get("installed", [])}
    ids.update(int(a) for a in index.get("owned", {}))
    return ids


def tool_impls(dispatch, log, jobs=None, on_stop_listening=None, voice=None,
               steam=None):
    """name -> fn(args: dict) -> dict. Shared by pipeline and REPL. jobs is
    the Tier-3 JobStore; None (REPL, or worker CLI missing) makes
    background_task refuse truthfully instead of pretending.
    on_stop_listening is GrammarGate.request_stop, and None refuses the same
    way: only a live voice session has a mic to close.

    voice=cfg["voice"] lets the store data lane be switched OFF: when
    steamDataTools is false, list_games/search_store are simply absent from the
    returned dict, and function_schemas renders only what's present - so the
    kill switch removes them from what the MODEL sees (selection pressure), not
    just from what it can call. None (REPL/bench) keeps every tool.

    steam=a SteamSession gates install_game and the download-status source the
    same way: absent or un-enrolled -> install_game is not offered at all,
    which is the token auto-gate (no config bool)."""
    def launch_game(args):
        appid = int(args.get("appid", 0))
        if appid not in known_appids():
            log.warn("tool_refused", tool="launch_game", reason="unknown_appid",
                     appid=appid)
            return {"ok": False, "error": f"appid {appid} is not in the catalog"}
        if library.installed_name(appid) is None:
            return {"ok": False, "error": "that game is owned but not "
                    "installed - installing needs the controller"}
        r = dispatch.play_game(appid)
        return {"ok": r.ok, "detail": r.detail}

    def quit_game(args):
        appid = int(args.get("appid", 0))
        if appid not in known_appids():
            log.warn("tool_refused", tool="quit_game", reason="unknown_appid",
                     appid=appid)
            return {"ok": False, "error": f"appid {appid} is not in the catalog"}
        r = dispatch.quit_game(appid)
        return {"ok": r.ok, "detail": r.detail}

    def install_game(args):
        """Get an owned-but-not-installed game downloading. TWO paths, tried in
        order, so this tool works with or without a credential:

        1. the account session queues it silently (nothing to press), when that
           lane is enrolled AND actually minting;
        2. otherwise put the game's Big Picture page on the TV and let the user
           press Install with the controller they are already holding.

        (2) is not a consolation prize - it is how anyone installs in Big
        Picture, and it needs no token, never expires, and cannot break when
        Valve changes an undocumented endpoint. It does need a live session,
        which is exactly the situation someone asking for this is in."""
        appid = int(args.get("appid", 0))
        if appid not in known_appids():
            log.warn("tool_refused", tool="install_game", reason="unknown_appid",
                     appid=appid)
            return {"ok": False, "error": f"appid {appid} is not in the catalog"}
        if library.installed_name(appid) is not None:
            return {"ok": False, "error": "that game is already installed"}
        if steam is not None and steam.available():
            try:
                r = steam.install(appid)
                if r.get("ok"):
                    return r
                log.warn("install_fallback", appid=appid, why=r.get("error"))
            except Exception as e:
                # available() proves the token is PRESENT, not that it still
                # mints (a web-audience token never does - 2026-08-14). Don't
                # break the turn and don't give up: fall through to the path
                # that needs no credential at all.
                log.error("install_error", appid=appid, err=str(e))
        r = dispatch.nav("details", appid)
        if r.ok:
            return {"ok": True, "detail": "it's on the TV now - press Install "
                    "and the download starts"}
        return {"ok": False, "error": r.detail}

    def nav(args):
        """Big Picture navigation. downloads/library/store need no appid;
        game_page needs an OWNED one, store_page any. Collections are a
        voice-grammar concept (they resolve by name on the box), not here."""
        target = args.get("target")
        appid = args.get("appid")
        if target == "game_page":
            # The LIBRARY page. Only a game they own has one, so the catalog
            # check is the right guard here.
            appid = int(appid or 0)
            if appid not in known_appids():
                log.warn("tool_refused", tool="nav", reason="unknown_appid",
                         appid=appid)
                return {"ok": False, "error": f"appid {appid} is not in the catalog"}
            r = dispatch.nav("details", appid)
        elif target == "store_page":
            # NO catalog check, deliberately: a store page is exactly where you
            # send someone for a game they do NOT own. "Open the store page for
            # Big Walk" was refused on the couch for that reason (2026-08-14).
            # A wrong appid costs one dud page on the TV; refusing costs the
            # feature - and this is now the hands-free path to installing,
            # since the install dialog needs a button press either way.
            appid = int(appid or 0)
            if appid <= 0:
                return {"ok": False, "error": "I need the game's store appid"}
            r = dispatch.nav("store", appid)
        elif target == "collection":
            # The grammar handles "show my roguelikes" when it hears the name
            # right. When it MISHEARS ("neck" for "mech"), the miss falls here -
            # and with no collection path this tool used to answer by navigating
            # to the library, three times in a row, while the user repeated
            # themselves (2026-08-14). So: resolve fuzzily, and on a miss hand
            # back the real names, which is a thing the model can act on.
            rows = library.load().get("collections", [])
            if not rows:
                return {"ok": False, "error": "no collections are synced yet - "
                        "the PC has to be awake for that"}
            cid = None
            want = str(args.get("collection") or "").strip()
            if want:
                import titles
                resolve = titles.build_collection_resolver(
                    (voice or {}).get("fuzzyTitleThreshold", 87))
                cid, _ = resolve(want) if resolve else (None, None)
            if cid is None:
                return {"ok": False,
                        "error": f"no collection matches {want!r}" if want
                                 else "which collection?",
                        "collections": [r["name"] for r in rows]}
            r = dispatch.nav("collection", cid)
        elif target in ("downloads", "library", "store"):
            r = dispatch.nav(target)
        else:
            return {"ok": False, "error": f"unknown nav target {target}"}
        return {"ok": r.ok, "detail": r.detail}

    plain = {"end_session": dispatch.end_session,
             "start_session": dispatch.start_session,
             "volume_up": dispatch.volume_up,
             "volume_down": dispatch.volume_down,
             "mute": dispatch.mute_toggle}

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
        """The one tool that acts on the CONVERSATION rather than the room.
        Not dry-run gated, unlike everything in dispatch.py: closing our own
        mic changes nothing on the TV or the PC, and a drill where "go away"
        is answered but never obeyed would be testing the wrong thing."""
        if on_stop_listening is None:
            return {"ok": False, "error": "there is no open voice session to "
                    "close - nothing is listening in the first place"}
        on_stop_listening()
        return {"ok": True, "detail": "going quiet once you've said your "
                "goodbye - the wake word is what reopens the mic"}

    def get_now_playing(args):
        r = dispatch.now_playing()
        if not r.ok:
            return {"ok": False, "error": r.detail}
        appid = int(r.detail) if str(r.detail).isdigit() else 0
        return {"ok": True, "appid": appid,
                "name": library.installed_name(appid) if appid else None}

    def get_game_details(args):
        appid = int(args.get("appid", 0))
        meta = library.load_meta().get(str(appid))
        name = library.installed_name(appid)
        installed = name is not None
        if not installed:
            o = library.load().get("owned", {}).get(str(appid))
            name = o.get("name") if o else None
        # Facets are opt-in and for ANY appid (not just owned) - a store name
        # comes from GetItems when the catalog can't supply one, so "tell me
        # about <a game I don't own>" works. The independent ones run CONCURRENTLY
        # (a multi-facet ask shouldn't serialize store round-trips into one
        # spoken turn); hltb runs after, since it needs a name that price - or a
        # fallback GetItems lookup - supplies. Each facet is INDEPENDENTLY
        # fail-soft: one that raises drops to None, it never sinks the call.
        # Facets are live store calls, so the steamDataTools kill switch gates
        # them too (same lane as list_games/search_store).
        store_on = voice is None or voice.get("steamDataTools", True)
        want = (args.get("facets") or []) if store_on else []
        tasks = {}
        if "price" in want:
            tasks["price"] = lambda: library._store_items([appid]).get(appid)
        if "reviews" in want:
            tasks["reviews"] = lambda: library.fetch_reviews(appid)
        if "news" in want:
            tasks["news"] = lambda: library.fetch_news(appid)
        facets = {}
        if tasks:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
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
            # Neither the catalog nor a requested price facet gave a name -
            # resolve it from the store. hltb NEEDS one ("how long to beat
            # <a game I don't own>" used to die as "unknown appid"), and
            # every facet WANTS one: four nameless review payloads in one
            # turn left the model matching results to titles from its own
            # bookkeeping, and it misattributed a superlative (2026-08-15).
            # A facet ask is already a live-store conversation, so one more
            # GetItems call is in kind - and the kill switch already zeroed
            # `want` if store calls are off.
            name = (library._store_items([appid]).get(appid) or {}).get("name")
        if "hltb" in want and name:
            facets["hltb"] = library.fetch_hltb(name)
        if not (meta or name or any(facets.values())):
            return {"ok": False, "error": "unknown appid"}
        return {"ok": True, "name": name, "installed": installed,
                **(meta or {}), **{k: v for k, v in facets.items() if v}}

    def list_games(args):
        """The feed reader: sale/trending/recent lists. wishlist_on_sale and
        specials come from the precomputed state/deals.json (~0 ms); trending
        and recently_played are cheap live calls. downloading goes over the
        account session (steam_session.py), which is optional and self-gates on
        its refresh token - so it refuses truthfully rather than erroring when
        that lane was never enrolled."""
        source = args.get("source")
        if source == "wishlist_on_sale":
            rows = library.load_deals().get("wishlist_on_sale")
            if rows is None:
                # Two honest causes for a missing key, don't guess between them:
                # no steamId64 (refresh_deals never writes the key), or the
                # first deals sync simply hasn't run yet.
                return {"ok": False, "error": "no wishlist data - either the "
                        "steamId64 isn't set, or the store sync hasn't run yet"}
            return {"ok": True, "source": source, "games": rows[:10]}
        if source == "specials":
            return {"ok": True, "source": source,
                    "games": library.load_deals().get("specials", [])[:10]}
        if source == "trending":
            return {"ok": True, "source": source, "games": library.fetch_trending()[:10]}
        if source == "recently_played":
            rows = library.fetch_recently_played()
            return {"ok": True, "source": source, "games": rows[:10]}
        if source == "downloading":
            if steam is None or not steam.available():
                return {"ok": False, "error": "download status isn't set up - the "
                        "account session hasn't been enrolled"}
            try:
                return {"ok": True, "source": source,
                        "games": steam.download_status()}
            except Exception as e:
                log.error("download_status_error", err=str(e))
                return {"ok": False, "error": "couldn't reach Steam for the "
                        "download status - the session may need re-enrolling"}
        return {"ok": False, "error": f"unknown source {source}"}

    def search_store(args):
        """Steam's own filtered search - facts, in the same breath. NOT the
        research worker (that's background_task, for judgment). Tag names come
        from the caller (spoken genres); unknown ones are dropped, term still
        applies."""
        term = str(args.get("term", "")).strip()
        tags = args.get("tags") or []
        if not term and not tags:
            return {"ok": False, "error": "search needs a term or a genre tag"}
        rows = library.fetch_store_search(
            term=term, tags=tags, max_price=args.get("max_price"),
            on_sale=bool(args.get("on_sale")))
        return {"ok": True, "count": len(rows), "games": rows}

    def background_task(args):
        task = str(args.get("task", "")).strip()
        if not task:
            return {"ok": False, "error": "background_task needs a task"}
        if jobs is None:
            return {"ok": False, "error": "background tasks aren't available "
                    "right now - answer from what you know instead"}
        # The user's words ride the gate's snapshot (see dispatch.Utterance).
        ok, detail = jobs.enqueue(task, asked=dispatch.utterance.asked)
        # The generic tool_call event comes from function_schemas now (one
        # home, every tool); this one keeps the TASK text, which the generic
        # args field truncates and which is what a job post-mortem needs.
        log("job_requested", ok=ok, task=task[:200])
        return {"ok": ok, "detail" if ok else "error": detail}

    impls = {"launch_game": launch_game, "control": control,
             "stop_listening": stop_listening,
             "get_now_playing": get_now_playing,
             "get_game_details": get_game_details,
             "background_task": background_task,
             "list_games": list_games, "search_store": search_store,
             "quit_game": quit_game, "nav": nav,
             "install_game": install_game}
    if voice is not None and not voice.get("steamDataTools", True):
        for gated in ("list_games", "search_store"):
            impls.pop(gated, None)
    # install_game is ALWAYS offered now: without the account session it falls
    # back to putting the game's page on the TV, so it always does something
    # useful. (It used to be dropped whenever the token was absent - which also
    # meant a PRESENT-but-dead token left it offered and broken, the exact state
    # the couch hit on 2026-08-14.)
    return impls


TOOL_DEFS = [
    ("launch_game", "Launch a game from the catalog by appid. Starts a "
     "session automatically if none is running - never call start_session "
     "first.",
     {"appid": {"type": "integer", "description": "appid from the catalog"}},
     ["appid"]),
    ("control", "Control the system: end_session, start_session, volume_up, "
     "volume_down, mute, set_volume (with level), switch_input "
     "(with input name). start_session returns while the session is still "
     "coming up - don't call nav in the same turn; say it's starting and "
     "let the user ask again.",
     {"action": {"type": "string",
                 "enum": ["end_session", "start_session", "volume_up",
                          "volume_down", "mute", "set_volume", "switch_input"]},
      "level": {"type": "integer",
                "description": "volume level for set_volume"},
      "input": {"type": "string",
                "description": "spoken input name for switch_input; valid "
                "names are in the system prompt"}},
     ["action"]),
    ("stop_listening", "Stop listening: close the mic and end the "
     "conversation. Call it when the user tells you to go away, stop "
     "listening, or leave them alone - usually because they want to talk to "
     "someone else in the room. This is NOT end_session: nothing on the TV "
     "changes and a running game is untouched. Say a short goodbye in the "
     "same turn - it is spoken first, and only then does the mic close. The "
     "wake word reopens it, so this costs the user nothing.", {}, []),
    ("get_now_playing", "What game is currently running, if any.", {}, []),
    ("get_game_details", "Details for one appid: tags/description/score from "
     "the catalog, plus any live facets you ask for. Request facets only when "
     "the question needs them - each is a live store call. 'price' = current "
     "price and discount; 'reviews' = review score and a few recent comments "
     "(for 'what are people saying', pass the DLC's own appid); 'news' = recent "
     "patch/update notes; 'hltb' = how many hours to beat. Works for games the "
     "user does NOT own too.",
     {"appid": {"type": "integer", "description": "appid (catalog, or a store "
                "appid for a game the user doesn't own)"},
      "facets": {"type": "array", "items": {"type": "string",
                 "enum": ["price", "reviews", "news", "hltb"]},
                 "description": "which live facets to fetch; omit for catalog "
                 "details only"}},
     ["appid"]),
    ("list_games", "Read a ready-made list of games. source: 'wishlist_on_sale' "
     "(the user's wishlist items currently discounted), 'specials' (today's "
     "featured store sales), 'trending' (most-played right now), "
     "'recently_played' (what the user played in the last two weeks), "
     "'downloading' (what's installing on the PC and how far along). Use this "
     "for 'anything on sale', 'what's on my wishlist', 'what's popular', 'what "
     "have I been playing', 'how far along is the download'. Leads with names "
     "and prices - not a research task.",
     {"source": {"type": "string",
                 "enum": ["wishlist_on_sale", "specials", "trending",
                          "recently_played", "downloading"]}},
     ["source"]),
    ("search_store", "Search the Steam store with filters and get back names + "
     "prices immediately - this is the fast, factual path for 'find me a <kind "
     "of> game [under $N] [on sale]'. Pass genres/features as tags (e.g. "
     "'Roguelike', 'Co-op'), a title fragment as term, a dollar cap as "
     "max_price. Use THIS, not background_task, when Steam's own filters can "
     "answer - only escalate to background_task when the user wants judgment "
     "about which is actually good.",
     {"term": {"type": "string", "description": "title words, or empty when "
               "searching purely by genre tags"},
      "tags": {"type": "array", "items": {"type": "string"},
               "description": "genre/feature tag names, e.g. ['Roguelike','Co-op']"},
      "max_price": {"type": "integer", "description": "dollar price ceiling"},
      "on_sale": {"type": "boolean", "description": "restrict to discounted"}},
     []),
    ("quit_game", "Quit the game that is currently running. This ENDS the game "
     "and can lose unsaved progress, so treat it as destructive: call it only "
     "when the user clearly tells you to quit or close the game now, and if "
     "there is ANY doubt, confirm first ('Quit Elden Ring?') and act only on a "
     "yes - never on a guess. The appid must be the running game "
     "(get_now_playing tells you which). This is NOT end_session and NOT the "
     "TV - only the game closes; Big Picture stays up. It also clears the way "
     "when a different game is blocking a launch.",
     {"appid": {"type": "integer", "description": "appid of the running game"}},
     ["appid"]),
    ("nav", "Navigate the Big Picture UI on the TV during a live session. "
     "target: 'downloads' (download queue), 'library' (library home), 'store' "
     "(store front page) - none need an appid; 'game_page' (a game's library "
     "page with its Play button - for 'show me <game>', OWNED games only) and "
     "'store_page' (any game's store page, owned or not - for 'open the store "
     "page for <game>', and the way to put a game the user wants to BUY or "
     "INSTALL on the TV so they can hit the button with the controller); "
     "'collection' shows one of the user's own library collections by name "
     "(pass it in `collection` - if the name doesn't match, the result lists "
     "the real ones, so use those rather than guessing again).",
     {"target": {"type": "string",
                 "enum": ["downloads", "library", "store", "game_page",
                          "store_page", "collection"]},
      "appid": {"type": "integer", "description": "required for game_page (must "
                "be owned) and store_page (any Steam appid)"},
      "collection": {"type": "string", "description": "collection name, for "
                     "target=collection"}},
     ["target"]),
    ("install_game", "Start downloading a game the user owns but hasn't "
     "installed yet - use this for 'install <game>'. It either queues the "
     "download on the PC outright or puts the game's page up on the TV for "
     "them to press Install; the result tells you which, so say what actually "
     "happened rather than assuming. Only for owned-but-not-installed titles "
     "(installed ones are a no-op). Confirm the title first if there's any "
     "doubt; downloads are large.",
     {"appid": {"type": "integer", "description": "appid of an owned, not-yet-"
                "installed game"}},
     ["appid"]),
    ("background_task", "Queue the background research agent ONLY when the "
     "user asks you to go away and report back later, or when the work "
     "truly takes many steps (compare reviews across sources, dig into "
     "something) - minutes, not seconds. A recommendation or a what's-new "
     "question is NOT this: use search_store / list_games / get_game_details "
     "and answer in the same breath instead. The split is facts vs judgment: "
     "if Steam's own filters or a review summary answer it, that's search_store "
     "or get_game_details, now; only 'which of these is actually good' or a "
     "cross-source comparison is background_task. When you DO escalate after a "
     "search, put the shortlist you already found INTO the task text so the "
     "agent deepens it rather than starting over. It is a full agent with web "
     "access and its own copy of the library, NOT restricted to the library "
     "the way you are. The result is announced aloud later. After queueing, "
     "tell the user you'll get back to them.",
     {"task": {"type": "string",
               "description": "A self-contained brief: what the user "
               "actually wants, plus any constraint THEY stated. Do not add "
               "constraints of your own, and never tell it to stay inside "
               "the library or list the library for it - the library is what "
               "the user ALREADY owns, so 'recommend games I don't own, "
               "using only my library' asks for an empty set. When they want "
               "something new, write 'exclude games already in the library' "
               "instead; the agent can read the library itself."}},
     ["task"]),
]


def function_schemas(impls, log=None):
    """Pipecat FunctionSchema list with auto-registering async handlers.
    Tool impls call blocking dispatch (ssh/serial) - run them off the event
    loop so audio and the Flux socket keep flowing during a tool call.

    This is also the ONE place every assistant tool call passes through, so it
    is where the call is RECORDED - see _record below. log=None (REPL, bench,
    tests) keeps the Loki half quiet; the span half self-gates on tracing."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema

    def _record(name, args, out, log=log):
        """Emit the tool call to both telemetry sinks.

        WHY BOTH, and why here (2026-08-14): neither system could answer "which
        tool did it call, with what?" - Pipecat's llm span carries the
        completion TEXT only, so a tool-calling turn traces as output:null, and
        Loki only had the one hand-rolled event inside background_task. A live
        search that returned junk took a local trace-mirror dig to explain.
        The span puts it in the tree beside the turn's timings; the EVENT is
        greppable, joins on turn, and outlives Langfuse's 30-day retention.
        Fail-soft on both: telemetry never costs a session.
        """
        try:
            tracing.tool_span(name, json.dumps(args)[:2000], json.dumps(out)[:2000])
        except Exception:
            pass
        if log:
            ok = out.get("ok") if isinstance(out, dict) else None
            log("tool_call", tool=name, ok=ok, args=json.dumps(args)[:300])

    def wrap(name, fn):
        async def handler(params):
            args = dict(params.arguments)
            try:
                out = await asyncio.to_thread(fn, args)
            except Exception as e:
                # The fail-soft backstop for the whole tool surface: an impl
                # that raises (a store shape drift, an expired-token mint
                # failure) must never leave result_callback uncalled - that
                # breaks the turn instead of answering. Log it and hand back a
                # spoken error so the assistant says something. LOG, not
                # print: this used to reach the console only, in the one
                # function whose job is to record every tool call, so a
                # production impl that raised reached neither couch.log nor
                # Loki - the tool_call event below said ok=False and nothing
                # said why.
                if log:
                    log.error("tool_error", tool=name, err=repr(e))
                else:
                    print(f"  [tool-error] {name}: {e!r}")   # REPL/bench: no logger
                out = {"ok": False, "error": "that didn't go through - "
                       "something upstream failed"}
            # After the call, so the span carries the RESULT too. The await
            # above suspends but does not lose the OTel context (contextvars
            # are per-task), so this still parents onto Pipecat's llm span.
            _record(name, args, out)
            await params.result_callback(out)
        return handler

    # Render only tools present in `impls` - the schema half of the gating
    # tool_impls' docstring describes (a dropped tool leaves the model's view,
    # not just its reach).
    return [FunctionSchema(name=n, description=d, properties=p, required=r,
                           handler=wrap(n, impls[n]))
            for n, d, p, r in TOOL_DEFS if n in impls]


# `names` filters to the tools actually present in a given impls set, so the
# REPL/bench renderers can't offer a tool that isn't callable (steam=None drops
# install_game; steamDataTools=false drops the store tools) - a bare call
# renders every TOOL_DEF, which is what the schema-shape tests check.
def anthropic_tools(names=None):
    return [{"name": n, "description": d,
             "input_schema": {"type": "object", "properties": p, "required": r}}
            for n, d, p, r in TOOL_DEFS if names is None or n in names]


def openai_tools(names=None):
    # Responses API tool shape is FLAT (name/parameters at top level) - the
    # nested {"function": {...}} form is chat-completions only.
    return [{"type": "function", "name": n, "description": d,
             "parameters": {"type": "object", "properties": p, "required": r}}
            for n, d, p, r in TOOL_DEFS if names is None or n in names]


def _user_location(voice):
    """Non-empty location fields -> the 'approximate' user_location dict.
    Both providers accept the identical shape; None when nothing is set."""
    loc = {k: v for k, v in voice["location"].items() if v}
    return {"type": "approximate", **loc} if loc else None


def server_tools(voice, provider):
    """Provider-NATIVE server-side tools (the provider executes them; nothing
    in tool_impls), appended to the request next to the TOOL_DEFS renders.
    Today: web search behind config.assistantWebSearch. The capability is
    neutral, the entry is per-provider - same split as anthropic_tools()/
    openai_tools(). Knob asymmetry is the vendors': Anthropic caps calls via
    max_uses; OpenAI has no cap knob, so cost control there is prompt-side
    plus search_context_size low (smallest/fastest retrieval)."""
    if not voice["assistantWebSearch"]:
        return []
    if provider == "openai":
        tool = {"type": "web_search", "search_context_size": "low"}
    else:
        tool = {"type": "web_search_20250305", "name": "web_search",
                "max_uses": voice["assistantSearchMaxUses"]}
    loc = _user_location(voice)
    if loc:
        tool["user_location"] = loc
    return [tool]


# --- provider backends: one turn (with its tool loop) -> reply text -----------
# System prompt, tool schemas, and tool impls are provider-neutral; only the
# request/response shape differs. Each backend holds its own conversation state.


class AnthropicBackend:
    key = "anthropicApiKey"

    def __init__(self, secrets, model, effort=None, voice=None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=secrets[self.key])
        self.model = model
        self.messages = []
        self.cache_note = ""
        self.server_tools = server_tools(voice, "anthropic") if voice else []

    def turn(self, system_text, user_text, impls):
        # cache_control on the system block caches tools+system together
        # (render order is tools -> system -> messages; a breakpoint covers
        # everything before it). CAVEAT: small models have a minimum cacheable
        # prefix (4096 tokens on Haiku 4.5) below which the marker silently
        # does nothing - the REPL prints cache w/r so w0/r0 is visible.
        system = [{"type": "text", "text": system_text,
                   "cache_control": {"type": "ephemeral"}}]
        self.messages.append({"role": "user", "content": user_text})
        spoken = []          # text carried across pause_turn continuations
        while True:
            resp = self.client.messages.create(
                model=self.model, max_tokens=400, system=system,
                messages=self.messages,
                tools=anthropic_tools(set(impls)) + self.server_tools)
            u = resp.usage
            self.cache_note = (
                f"cache w{getattr(u, 'cache_creation_input_tokens', 0) or 0}"
                f"/r{getattr(u, 'cache_read_input_tokens', 0) or 0}")
            self.messages.append({"role": "assistant", "content": resp.content})
            text = " ".join(b.text for b in resp.content if b.type == "text")
            if resp.stop_reason == "pause_turn":
                # A long server-side search paused the turn mid-flight; the
                # documented contract is to re-send the partial assistant
                # content as-is and let the model continue. Server tool
                # blocks need no client-side result - only the accumulated
                # text matters to the caller.
                if text:
                    spoken.append(text)
                continue
            if resp.stop_reason != "tool_use":
                return " ".join(spoken + [text]) if spoken else text
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    out = impls[b.name](dict(b.input))
                    print(f"  [tool] {b.name}({dict(b.input)}) -> {out}")
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out)})
            self.messages.append({"role": "user", "content": results})


class OpenAIBackend:
    """Responses API - the interface OpenAI recommends for reasoning models;
    reasoning and tool calls coexist here (they don't cleanly on the legacy
    chat-completions endpoint). State is server-side via previous_response_id,
    which also threads reasoning items across tool calls for us."""
    key = "openaiApiKey"

    def __init__(self, secrets, model, effort="low", voice=None):
        import openai
        self.client = openai.OpenAI(api_key=secrets[self.key])
        self.model = model
        self.effort = effort            # none|minimal|low|medium|high (model-dep)
        self.prev = None
        self.cache_note = ""
        self.messages = []              # trace mirror; real state is server-side
        self.server_tools = server_tools(voice, "openai") if voice else []

    def turn(self, system_text, user_text, impls):
        self.messages.append({"role": "user", "content": user_text})
        pending = [{"role": "user", "content": user_text}]
        while True:
            resp = self.client.responses.create(
                model=self.model, instructions=system_text, input=pending,
                tools=openai_tools(set(impls)) + self.server_tools,
                reasoning={"effort": self.effort},
                max_output_tokens=1500, previous_response_id=self.prev)
            self.prev = resp.id
            det = getattr(resp.usage, "input_tokens_details", None)
            self.cache_note = (
                f"cache r{getattr(det, 'cached_tokens', 0) or 0}" if det else "")
            # Server-side searches are ITEMS in resp.output, not function
            # calls: filtering to function_call drops them entirely and the
            # trace then carries no evidence a lookup happened. That is not
            # cosmetic - a model's own account of itself is not evidence,
            # since it cannot tell afterwards whether a server-side tool ran.
            for o in resp.output:
                if "search" in (getattr(o, "type", "") or ""):
                    action = getattr(o, "action", None)
                    self.messages.append({
                        "role": "tool", "name": getattr(o, "type", "search"),
                        "query": getattr(action, "query", None) or str(action or "")[:200],
                        "status": getattr(o, "status", None)})
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                self.messages.append({"role": "assistant",
                                      "content": resp.output_text})
                return resp.output_text
            pending = []
            for c in calls:
                args = json.loads(c.arguments or "{}")
                out = impls[c.name](args)
                print(f"  [tool] {c.name}({args}) -> {out}")
                self.messages.append({"role": "tool", "name": c.name,
                                      "args": args, "out": out})
                pending.append({"type": "function_call_output",
                                "call_id": c.call_id, "output": json.dumps(out)})


BACKENDS = {"anthropic": AnthropicBackend, "openai": OpenAIBackend}


# Model per vendor, spelled out in config - both stay populated so flipping
# assistantProvider is the whole A/B, and neither lane hides behind a default.
MODEL_KEY = {"anthropic": "assistantModelAnthropic",
             "openai": "assistantModelOpenai"}


def default_model(cfg, provider):
    return cfg["voice"][MODEL_KEY[provider]]


def repl(cfg, secrets, log, dry_run=True, provider=None, model=None, effort=None):
    """--text mode: type transcripts, see replies + tool calls + latency. The
    20-query canned set and the model A/B run through this - same system prompt,
    tool schemas, and impls as the voice pipeline, either provider. Pick with
    --provider anthropic|openai [--model <id>] [--effort none|low|medium|high]."""
    from dispatch import Dispatch

    provider = provider or cfg["voice"]["assistantProvider"]
    if provider not in BACKENDS:
        print(f"unknown provider '{provider}' - one of {list(BACKENDS)}")
        return 2
    keyname = BACKENDS[provider].key
    if not cglib.real_key(secrets.get(keyname)):
        print(f"{keyname} is a placeholder - add it to secrets.json for {provider}")
        return 1

    impls = tool_impls(Dispatch(cfg, log, dry_run=dry_run), log)
    effort = effort or cfg["voice"]["assistantReasoningEffort"]
    backend = BACKENDS[provider](secrets, model or default_model(cfg, provider),
                                 effort=effort, voice=cfg["voice"])
    system_text = system_instruction(cfg)
    tag = f"{provider}/{backend.model}"
    if provider == "openai":
        tag += f" effort={effort}"
    if backend.server_tools:
        tag += " +websearch"
    print(f"assistant REPL - {tag}, dry_run={dry_run}. Empty line to quit.")
    try:
        while True:
            try:
                q = input("you> ").strip()
            except EOFError:
                break
            if not q:
                break
            t0 = time.time()
            try:
                text = backend.turn(system_text, q, impls)
            except Exception as e:
                # A bad knob value (e.g. an unsupported reasoning effort) or a
                # transient API error shouldn't kill the bench session - the
                # error text is the answer to the probe.
                print(f"API error ({time.time() - t0:.1f}s)> {e}")
                continue
            note = f", {backend.cache_note}" if backend.cache_note else ""
            print(f"assistant ({time.time() - t0:.1f}s{note})> {text}")
    finally:
        # Ctrl-C included: a bench conversation worth having is worth keeping.
        traces.save(f"repl-{provider}", backend.messages,
                    {"model": backend.model, "dry_run": dry_run})
    return 0
