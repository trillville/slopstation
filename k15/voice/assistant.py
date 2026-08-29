"""Assistant lane: system prompt, tool schemas, and tool impls.

Shared by the Pipecat pipeline (voice) and assistant_repl.py (the --text
bench); every tool routes through the same dispatch.py as the grammar gate.
An appid that isn't in the index is refused at the tool boundary.
"""
import json
import time

import cglib
import library
import steamstore
# tool spans; the module self-gates: REPL/bench are no-ops
import tracing

WEB_SEARCH_RULE = """\
You can search the web for current facts the catalog can't answer (release
dates, game news, prices, and games the user does not own). Search only
when the catalog genuinely can't answer, and keep the reply to two short
sentences. Never announce or offer to search - just search and state the
result. Your reply is read aloud by TTS: state facts in plain words with NO
citations, links, URLs, source names, or parenthetical references of any
kind - a bracketed source would be spoken letter by letter."""


RULES = """\
You are the voice assistant for a couch gaming setup (Steam on a TV).
Answers are SPOKEN aloud: plain text only, no markdown, no emoji, at most
two short sentences unless asked for detail. For list questions lead with
the count, name at most three (installed first, then most played), and
offer the rest.

The catalog below is the user's own library - what they ALREADY own.
Questions about games they do not own (what to buy, what's new, what's like
this one) are normal and among the most useful things you do: look them up
and answer NOW, in the same breath - that is a normal answer, not a
research project. For those look-ups you have tools: search_store to find a
kind of game by genre and price, list_games for what's on sale or trending
or what you've been playing, and get_game_details facets for a game's
price, reviews, patch news, or how long it takes to beat. Those answer facts
now. When the question is about ONE named game's reviews, price, updates or
length, get_game_details is the answer and web search is not: Steam's own
review score and patch notes are better than a search result and arrive
instantly. Search the web for what Steam does not carry.

Name titles from the catalog or from a tool result rather than from memory,
and when the ask is for something NEW, never offer a game that is already
in the catalog. And if you are asked later where an answer came from, do
not reconstruct your own process from guesswork: you cannot reliably tell
afterwards whether you looked something up, so say that plainly rather than
inventing a source or disowning a good one. A superlative needs the
numbers: never call one game the best, highest-rated, or most recent of a
set unless you have the figure for every candidate from this conversation -
one lookup cannot rank a list, so fetch the rest or name the one you
actually checked.

Use tools for every action; appids come only from the catalog. Tell a
QUESTION ABOUT an action apart from an INSTRUCTION to take it. 'What's the
command to end the session', 'what happens if I say that', 'how do I get
back to my desk' are questions: answer them and call no tool. Act only when
the user is telling you to do it now. If you can't tell which it is, answer
and offer ('want me to do that now?') - a needless sentence costs nothing,
a needless action ends someone's game. Ending the session and switching
input both interrupt what is on the TV, so never take either as a guess.
'Back to the office', 'back to my desk' and 'I'm done playing' mean END THE
SESSION - the office is the desk setup, not a TV input, and the only valid
input names are listed below. 'Stop listening', 'go away' and 'leave us
alone' are the opposite ask and cost nothing: call stop_listening, which
closes the mic and touches nothing else - never end the gaming session for
them.

You hear the user through speech-to-text, so expect mishears: 'met games'
is probably 'mech games', 'bolder's gate' is Baldur's Gate, 'dead lock' is
Deadlock. When a request reads odd, find the near-sounding reading that
best fits the catalog and the conversation and answer THAT, opening with
your reading so a wrong guess is self-correcting ('Mech games? You have
three...'). Ask one short clarifying question only when no reading clearly
wins. That rule resolves what to SAY; for an action, an unclear reading
means ask, never act on the best guess. If nothing in the catalog is close,
say the game isn't in the library - don't force a match. If something
fails, say so plainly.

Media downloads are large actions. Resolve a movie or series with find_media
first, use only the TMDB or TVDB id it returns, and ask one short clarifying
question when the intended result is not clear. Never guess an id or expose
torrent release names. A quality preference applies only to that request;
omit it to use the configured default."""


def system_instruction(cfg):
    """RULES + a config-derived tail (date, spoken input names, volume
    ceiling, mute semantics) + the catalog. Built once per session, so none
    of it moves under the prompt cache."""
    voice = cfg["voice"]
    inputs = voice.get("inputs", {})
    # A day with no zone resolves toward UTC: from 5pm Pacific on, briefs went
    # out dated tomorrow (2026-08-13). Empty timezone is a normal deployment.
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
WEB_SEARCH_RULE)
    return (RULES + " " + " ".join(tail) + "\n\n"
            "CATALOG (appid|name|tags|genres|hours|lastPlayed YYYY-MM-DD or "
            "never|inst/notinst|controller full/partial/none/?):\n"
            + "\n".join(library.catalog_lines()))


def known_appids():
    index = library.load()
    ids = {r["appid"] for r in index.get("installed", [])}
    ids.update(int(a) for a in index.get("owned", {}))
    return ids


def tool_impls(dispatch, log, operations=None, on_stop_listening=None,
               voice=None, steam=None, media=None):
    """name -> fn(args: dict) -> dict. Shared by pipeline and REPL.

    operations is the durable external-work ledger and on_stop_listening is
    GrammarGate.request_stop.
    voice=cfg["voice"] with steamDataTools false drops list_games/
    search_store, and function_schemas renders only what's present - so the
    kill switch removes them from what the MODEL sees, not just from what it
    can call; None (REPL/bench) keeps every tool. steam is a SteamSession,
    used by install_game and the download-status source. media is the
    Radarr/Sonarr request boundary; None removes every media tool."""
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
            return {"ok": False, "error": "that game is owned but not "
                    "installed - installing needs the controller"}
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
        if steam is not None and steam.available():
            try:
                r = steam.install(appid)
                if r.get("ok"):
                    if operations is not None:
                        owned = library.load().get("owned", {}).get(str(appid), {})
                        title = owned.get("name") or f"app {appid}"
                        try:
                            operation = operations.track_steam_install(
                                appid, title, turn=dispatch.utterance.turn,
                                verified=bool(r.get("verified")))
                            return {**r, "operation_id": operation["id"]}
                        except Exception as e:
                            # Submission already happened; tracking must not
                            # turn a successful external action into a refusal.
                            log.error("operation_track_failed", appid=appid,
                                      err=str(e))
                    return r
                log.warn("install_fallback", appid=appid, why=r.get("error"))
            except Exception as e:
                # available() proves the token is PRESENT, not that it still
                # mints (a web-audience token never does - 2026-08-14). Fall
                # through to the path that needs no credential.
                log.error("install_error", appid=appid, err=str(e))
        r = dispatch.nav("details", appid)
        if r.ok:
            return {"ok": True, "detail": "it's on the TV now - press Install "
                    "and the download starts"}
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
            # No catalog check: a store page is for a game they do NOT own,
            # and a catalog check refused exactly that ask (2026-08-14).
            appid = int(appid or 0)
            if appid <= 0:
                return {"ok": False, "error": "I need the game's store appid"}
            r = dispatch.nav("store", appid)
        elif target == "collection":
            # Grammar mishears land here (2026-08-14): resolve fuzzily, and on
            # a miss hand back the real names for the model to act on.
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
        """Acts on the CONVERSATION rather than the room. Not dry-run gated,
        unlike everything in dispatch.py: closing our own mic changes nothing
        on the TV or the PC."""
        if on_stop_listening is None:
            return {"ok": False, "error": "there is no open voice session to "
                    "close - nothing is listening in the first place"}
        on_stop_listening()
        return {"ok": True, "detail": "going quiet once you've said your "
                "goodbye - the wake word is what reopens the mic"}

    def get_now_playing(args):
        # The PC reports RunningAppID 0 all through a launch, which read as
        # "nothing is playing" while start_session said "already starting"
        # (2026-08-21, turn 0b785e). session_active is the same predicate that
        # refusal uses (cglib.session_active), so the two cannot disagree.
        active = cglib.session_active()
        r = dispatch.now_playing()
        if not r.ok:
            # Mid-launch the PC can be unreachable; the lock still answers.
            return {"ok": False, "error": r.detail, "session_active": active}
        appid = int(r.detail) if str(r.detail).isdigit() else 0
        return {"ok": True, "appid": appid,
                "name": library.installed_name(appid) if appid else None,
                "session_active": active}

    def get_game_details(args):
        appid = int(args.get("appid", 0))
        meta = library.load_meta().get(str(appid))
        name = library.installed_name(appid)
        installed = name is not None
        if not installed:
            o = library.load().get("owned", {}).get(str(appid))
            name = o.get("name") if o else None
        # Facets are opt-in and work for ANY appid, not just owned. The
        # independent ones run concurrently; hltb runs after, since it needs
        # the name price (or a fallback GetItems lookup) supplies. Each facet
        # is fail-soft: one that raises drops to None. Live store calls, so
        # steamDataTools gates them too.
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
            # hltb needs a name, and nameless facet payloads let the model
            # misattribute results across titles (2026-08-15).
            name = (steamstore.store_items([appid]).get(appid) or {}).get("name")
        if "hltb" in want and name:
            facets["hltb"] = steamstore.fetch_hltb(name)
        if not (meta or name or any(facets.values())):
            return {"ok": False, "error": "unknown appid"}
        return {"ok": True, "name": name, "installed": installed,
                **(meta or {}), **{k: v for k, v in facets.items() if v}}

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
                return {"ok": False, "error": "no wishlist data - either the "
                        "steamId64 isn't set, or the store sync hasn't run yet"}
            return {"ok": True, "source": source, "games": rows[:10]}
        if source == "specials":
            return {"ok": True, "source": source,
                    "games": steamstore.load_deals().get("specials", [])[:10]}
        if source == "trending":
            return {"ok": True, "source": source, "games": steamstore.fetch_trending()[:10]}
        if source == "recently_played":
            rows = steamstore.fetch_recently_played()
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
        """Steam's own filtered search. Tag names come from the caller (spoken
        genres); unknown ones are dropped, term still applies."""
        term = str(args.get("term", "")).strip()
        tags = args.get("tags") or []
        if not term and not tags:
            return {"ok": False, "error": "search needs a term or a genre tag"}
        rows = steamstore.fetch_store_search(
            term=term, tags=tags, max_price=args.get("max_price"),
            on_sale=bool(args.get("on_sale")))
        return {"ok": True, "count": len(rows), "games": rows}

    def list_operations(args):
        scope = args.get("scope", "active")
        if scope not in ("active", "recent"):
            return {"ok": False, "error": f"unknown operation scope {scope}"}
        return {"ok": True, "scope": scope,
                "operations": operations.for_assistant(
                    scope, acknowledge=(scope == "recent"))}

    def find_media(args):
        kind = str(args.get("kind", ""))
        try:
            candidates = media.find(kind, args.get("query", ""))
            return {"ok": True, "kind": kind, "candidates": candidates}
        except Exception as e:
            log.error("tool_error", tool="find_media", err=str(e))
            return {"ok": False, "error": str(e)}

    def _track_media(submission):
        if submission.get("already_available") or operations is None:
            return submission
        metadata = {k: submission.get(k) for k in
                    ("catalog_id", "preset", "profile", "seasons",
                     "baseline_file_id")
                    if k in submission}
        try:
            operation = operations.track_external(
                submission["kind"], submission["authority"],
                submission["external_ref"], submission["title"],
                turn=dispatch.utterance.turn,
                detail=f"{submission['authority'].title()} accepted the request",
                metadata=metadata)
            return {**submission, "operation_id": operation["id"]}
        except Exception as e:
            # The external mutation already succeeded; tracking cannot turn it
            # into a refusal or cause the model to submit it twice.
            log.error("tool_error", tool="track_media", err=str(e))
            return {**submission, "tracking": "failed"}

    def request_movie(args):
        try:
            tmdb_id = int(args.get("tmdb_id", 0))
            preset = args.get("preset", "default")
            if tmdb_id <= 0:
                return {"ok": False, "error": "tmdb_id must be positive"}
            if dispatch.dry_run:
                detail = f"would request TMDB {tmdb_id} with preset {preset}"
                log("dry_run_would", action=detail)
                return {"ok": True, "dry_run": True, "detail": detail}
            return _track_media(media.request_movie(tmdb_id, preset))
        except Exception as e:
            log.error("tool_error", tool="request_movie", err=str(e))
            return {"ok": False, "error": str(e)}

    def request_series(args):
        try:
            tvdb_id = int(args.get("tvdb_id", 0))
            preset = args.get("preset", "default")
            seasons = args.get("seasons")
            if tvdb_id <= 0:
                return {"ok": False, "error": "tvdb_id must be positive"}
            if dispatch.dry_run:
                detail = f"would request TVDB {tvdb_id} with preset {preset}"
                log("dry_run_would", action=detail)
                return {"ok": True, "dry_run": True, "detail": detail}
            return _track_media(media.request_series(tvdb_id, preset, seasons))
        except Exception as e:
            log.error("tool_error", tool="request_series", err=str(e))
            return {"ok": False, "error": str(e)}

    impls = {"launch_game": launch_game, "control": control,
             "stop_listening": stop_listening,
             "get_now_playing": get_now_playing,
             "get_game_details": get_game_details,
             "list_games": list_games, "search_store": search_store,
             "quit_game": quit_game, "nav": nav,
             "install_game": install_game}
    if operations is not None:
        impls["list_operations"] = list_operations
    if media is not None:
        impls.update(find_media=find_media, request_movie=request_movie,
                     request_series=request_series)
    if voice is not None and not voice.get("steamDataTools", True):
        for gated in ("list_games", "search_store"):
            impls.pop(gated, None)
    # install_game is always offered: without the account session it falls
    # back to putting the game's page on the TV, so it always does something.
    return impls


# What the model is told each tool is for. The table below pairs each with
# its schema; the words are the whole interface, so they read as prose.
_LAUNCH_GAME = """\
Launch a game from the catalog by appid. Starts a session automatically if
none is running - never call start_session first."""

_CONTROL = """\
Control the system: end_session, start_session, volume_up, volume_down,
mute, set_volume (with level), switch_input (with input name).
start_session returns while the session is still coming up - don't call nav
in the same turn; say it's starting and let the user ask again."""

_STOP_LISTENING = """\
Stop listening: close the mic and end the conversation. Call it when the
user tells you to go away, stop listening, or leave them alone - usually
because they want to talk to someone else in the room. This is NOT
end_session: nothing on the TV changes and a running game is untouched. Say
a short goodbye in the same turn - it is spoken first, and only then does
the mic close. The wake word reopens it, so this costs the user nothing."""

_GET_NOW_PLAYING = """\
What game is currently running, if any. session_active is the rig's own
busy state: true with appid 0 means a session is STARTING (a launch can
take a minute before anything shows on the TV) or Big Picture is up with no
game - either way the rig is busy, so never report it as idle and never
offer to start a session. false means truly idle."""

_GET_GAME_DETAILS = """\
Details for one appid: tags/description/score from the catalog, plus any
live facets you ask for. Request facets only when the question needs them -
each is a live store call. 'price' = current price and discount; 'reviews'
= review score and a few recent comments (for 'what are people saying',
pass the DLC's own appid); 'news' = recent patch/update notes; 'hltb' = how
many hours to beat. Works for games the user does NOT own too."""

_LIST_GAMES = """\
Read a ready-made list of games. source: 'wishlist_on_sale' (the user's
wishlist items currently discounted), 'specials' (today's featured store
sales), 'trending' (most-played right now), 'recently_played' (what the
user played in the last two weeks), 'downloading' (what's installing on the
PC and how far along). Use this for 'anything on sale', 'what's on my
wishlist', 'what's popular', 'what have I been playing', 'how far along is
the download'. Leads with names and prices - not a research task."""

_SEARCH_STORE = """\
Search the Steam store with filters and get back names + prices immediately
- this is the fast, factual path for 'find me a <kind of> game [under $N]
[on sale]'. Pass genres/features as tags (e.g. 'Roguelike', 'Co-op'), a
title fragment as term, a dollar cap as max_price. Use this when Steam's own
filters can answer."""

_QUIT_GAME = """\
Quit the game that is currently running. This ENDS the game and can lose
unsaved progress, so treat it as destructive: call it only when the user
clearly tells you to quit or close the game now, and if there is ANY doubt,
confirm first ('Quit Elden Ring?') and act only on a yes - never on a
guess. The appid must be the running game (get_now_playing tells you
which). This is NOT end_session and NOT the TV - only the game closes; Big
Picture stays up. It also clears the way when a different game is blocking
a launch."""

_NAV = """\
Navigate the Big Picture UI on the TV during a live session. target:
'downloads' (download queue), 'library' (library home), 'store' (store
front page) - none need an appid; 'game_page' (a game's library page with
its Play button - for 'show me <game>', OWNED games only) and 'store_page'
(any game's store page, owned or not - for 'open the store page for
<game>', and the way to put a game the user wants to BUY or INSTALL on the
TV so they can hit the button with the controller); 'collection' shows one
of the user's own library collections by name (pass it in `collection` - if
the name doesn't match, the result lists the real ones, so use those rather
than guessing again)."""

_INSTALL_GAME = """\
Start downloading a game the user owns but hasn't installed yet - use this
for 'install <game>'. It either queues the download on the PC outright or
puts the game's page up on the TV for them to press Install; the result
tells you which, so say what actually happened rather than assuming. Only
for owned-but-not-installed titles (installed ones are a no-op). Confirm
the title first if there's any doubt; downloads are large."""

_LIST_OPERATIONS = """\
Read Slopstation's durable operations. Use scope 'active' for current work and
'recent' for what just finished or what an announcement referred to. These
records come from explicit external observations; do not infer completion from
conversation history or from an absent download."""

_FIND_MEDIA = """\
Resolve a movie or series title before requesting it. Returns at most five
canonical candidates with year and a TMDB movie id or TVDB series id. Use the
returned id in a request tool only when the intended candidate is clear; ask a
short clarifying question otherwise."""

_REQUEST_MOVIE = """\
Request one movie by a tmdb_id returned by find_media. preset is default,
1080p, or 2160p; omit it unless the user gives a quality preference. This can
start a large download, so call it only for an explicit request and never with
a guessed id."""

_REQUEST_SERIES = """\
Request one series by a tvdb_id returned by find_media. Omit seasons for all
normal seasons, or pass explicit positive season numbers. preset is default,
1080p, or 2160p. This can start many large downloads, so call it only for an
explicit request and never with a guessed id."""

TOOL_DEFS = [
    ("launch_game", _LAUNCH_GAME,
     {"appid": {"type": "integer", "description": "appid from the catalog"}},
     ["appid"]),
    ("control", _CONTROL,
     {"action": {"type": "string",
                 "enum": ["end_session", "start_session", "volume_up",
                          "volume_down", "mute", "set_volume", "switch_input"]},
      "level": {"type": "integer",
                "description": "volume level for set_volume"},
      "input": {"type": "string",
                "description": "spoken input name for switch_input; valid "
                "names are in the system prompt"}},
     ["action"]),
    ("stop_listening", _STOP_LISTENING, {}, []),
    ("get_now_playing", _GET_NOW_PLAYING, {}, []),
    ("get_game_details", _GET_GAME_DETAILS,
     {"appid": {"type": "integer", "description": "appid (catalog, or a store "
                "appid for a game the user doesn't own)"},
      "facets": {"type": "array", "items": {"type": "string",
                 "enum": ["price", "reviews", "news", "hltb"]},
                 "description": "which live facets to fetch; omit for catalog "
                 "details only"}},
     ["appid"]),
    ("list_games", _LIST_GAMES,
     {"source": {"type": "string",
                 "enum": ["wishlist_on_sale", "specials", "trending",
                          "recently_played", "downloading"]}},
     ["source"]),
    ("search_store", _SEARCH_STORE,
     {"term": {"type": "string", "description": "title words, or empty when "
               "searching purely by genre tags"},
      "tags": {"type": "array", "items": {"type": "string"},
               "description": "genre/feature tag names, e.g. ['Roguelike','Co-op']"},
      "max_price": {"type": "integer", "description": "dollar price ceiling"},
      "on_sale": {"type": "boolean", "description": "restrict to discounted"}},
     []),
    ("quit_game", _QUIT_GAME,
     {"appid": {"type": "integer", "description": "appid of the running game"}},
     ["appid"]),
    ("nav", _NAV,
     {"target": {"type": "string",
                 "enum": ["downloads", "library", "store", "game_page",
                          "store_page", "collection"]},
      "appid": {"type": "integer", "description": "required for game_page (must "
                "be owned) and store_page (any Steam appid)"},
      "collection": {"type": "string", "description": "collection name, for "
                     "target=collection"}},
     ["target"]),
    ("install_game", _INSTALL_GAME,
     {"appid": {"type": "integer", "description": "appid of an owned, not-yet-"
                 "installed game"}},
     ["appid"]),
    ("list_operations", _LIST_OPERATIONS,
     {"scope": {"type": "string", "enum": ["active", "recent"]}},
     []),
    ("find_media", _FIND_MEDIA,
     {"kind": {"type": "string", "enum": ["movie", "series"]},
      "query": {"type": "string",
                "description": "spoken title and optional year"}},
     ["kind", "query"]),
    ("request_movie", _REQUEST_MOVIE,
     {"tmdb_id": {"type": "integer",
                  "description": "id returned by find_media"},
      "preset": {"type": "string",
                 "enum": ["default", "1080p", "2160p"]}},
     ["tmdb_id"]),
    ("request_series", _REQUEST_SERIES,
     {"tvdb_id": {"type": "integer",
                  "description": "id returned by find_media"},
      "preset": {"type": "string",
                 "enum": ["default", "1080p", "2160p"]},
      "seasons": {"type": "array", "items": {"type": "integer"},
                  "description": "positive season numbers; omit for all normal seasons"}},
     ["tvdb_id"]),
]


def function_schemas(impls, log=None):
    """Pipecat FunctionSchema list with auto-registering async handlers. Tool
    impls call blocking dispatch (ssh/serial) - run them off the event loop so
    audio and the Flux socket keep flowing. Also the one place every tool call
    is recorded (_record); log=None keeps the Loki half quiet."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema

    def _record(name, args, out, log=log):
        """Emit the tool call to both telemetry sinks. Pipecat's llm span
        carries the completion TEXT only, so a tool-calling turn traces as
        output:null; the event is greppable, joins on turn, and outlives
        Langfuse's 30-day retention. Fail-soft on both."""
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
                # Fail-soft backstop for the whole tool surface: an impl that
                # raises must never leave result_callback uncalled - that
                # breaks the turn instead of answering.
                print(f"  [tool-error] {name}: {e!r}")
                if log:
                    log.error("tool_error", tool=name, err=repr(e))
                out = {"ok": False, "error": "that didn't go through - "
                       "something upstream failed"}
            # After the call, so the span carries the RESULT. The await above
            # does not lose the OTel context (contextvars are per-task), so
            # this still parents onto Pipecat's llm span.
            _record(name, args, out)
            await params.result_callback(out)
        return handler

    # Render only tools present in `impls` - the schema half of tool_impls'
    # gating.
    return [FunctionSchema(name=n, description=d, properties=p, required=r,
                           handler=wrap(n, impls[n]))
            for n, d, p, r in TOOL_DEFS if n in impls]


# `names` filters to the tools present in a given impls set, so a renderer
# can't offer a tool that isn't callable; None renders every TOOL_DEF.
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
        tool = {"type": "web_search_20250305", "name": "web_search",
                "max_uses": voice["assistantSearchMaxUses"]}
    loc = _user_location(voice)
    if loc:
        tool["user_location"] = loc
    return [tool]


# Both keys stay populated in config so flipping assistantProvider is the
# whole A/B, and neither lane hides behind a default.
MODEL_KEY = {"anthropic": "assistantModelAnthropic",
             "openai": "assistantModelOpenai"}
PROVIDER_KEY = {"anthropic": "anthropicApiKey", "openai": "openaiApiKey"}


def default_model(voice, provider):
    return voice[MODEL_KEY[provider]]
