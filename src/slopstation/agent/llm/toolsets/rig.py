"""Tools for the rig itself: the session, the TV, the mic, and Big Picture."""

from slopstation import sessionlock
from slopstation.agent.llm.registry import ToolContext, ToolSpec
from slopstation.agent.tools import library

LAUNCH_GAME = """\
Launch a game from the catalog by appid. Starts a session automatically if
none is running - never call start_session first."""

SESSION = """\
Control the session: end_session, start_session, switch_input (with input
name; the valid names are in the system prompt). Ending the session and
switching input both interrupt what is on the TV, so never take either as a
guess. start_session returns while the session is still coming up - don't
call nav in the same turn; say it's starting and let the user ask again."""

VOLUME = """\
Adjust the TV volume: up, down, set (with level), mute (a toggle). Harmless -
none of these interrupt what is on the TV. The range and the mute caveat are
in the system prompt; confirm the level the tool actually returns."""

STOP_LISTENING = """\
Stop listening: close the mic and end the conversation. Call it when the
user tells you to go away, stop listening, or leave them alone - usually
because they want to talk to someone else in the room. This is NOT
end_session: nothing on the TV changes and a running game is untouched. Do
not say anything with it: the mic closes as the call lands and a sleep tone
tells the user. The wake word reopens it, so this costs the user nothing."""

GET_NOW_PLAYING = """\
What game is currently running, if any. session_active is the rig's own
busy state. launching true means a launch is still in progress (it can take
a minute and may be retrying): say so, offer to wait or to cancel, and never
call it an active session. session_active true with launching false means
Big Picture is up (appid 0) or a game is running. Either way the rig is
busy: never report it as idle or offer to start a session. false means truly
idle."""

QUIT_GAME = """\
Quit the game that is currently running. This ENDS the game and can lose
unsaved progress, so treat it as destructive: call it only when the user
clearly tells you to quit or close the game now, and if there is ANY doubt,
confirm first ('Quit Elden Ring?') and act only on a yes - never on a
guess. The appid must be the running game (get_now_playing tells you
which). This is NOT end_session and NOT the TV - only the game closes; Big
Picture stays up. It also clears the way when a different game is blocking
a launch."""

NAV = """\
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

INSTALL_GAME = """\
Start downloading a game the user owns but hasn't installed yet - use this
for 'install <game>'. It either queues the download on the PC outright or
puts the game's page up on the TV for them to press Install; the result
tells you which, so say what actually happened rather than assuming. Only
for owned-but-not-installed titles (installed ones are a no-op). Confirm
the title first if there's any doubt; downloads are large."""

SPECS = [
    ToolSpec(
        "launch_game",
        LAUNCH_GAME,
        {"appid": {"type": "integer", "description": "appid from the catalog"}},
        ("appid",),
        risk="act",
        area="session",
        keywords=("play", "launch", "start game", "open game", "run"),
    ),
    ToolSpec(
        "session",
        SESSION,
        {
            "action": {
                "type": "string",
                "enum": ["end_session", "start_session", "switch_input"],
            },
            "input": {
                "type": "string",
                "description": "spoken input name for switch_input; valid "
                "names are in the system prompt",
            },
        },
        ("action",),
        risk="act",
        area="session",
        keywords=(
            "end session",
            "back to the office",
            "done playing",
            "start session",
            "turn on",
            "switch input",
            "hdmi",
            "apple tv",
        ),
    ),
    ToolSpec(
        "volume",
        VOLUME,
        {
            "action": {"type": "string", "enum": ["up", "down", "set", "mute"]},
            "level": {"type": "integer", "description": "volume level for set"},
        },
        ("action",),
        risk="act",
        area="session",
        keywords=("volume", "louder", "quieter", "mute", "sound", "turn it down"),
    ),
    ToolSpec(
        "stop_listening",
        STOP_LISTENING,
        {},
        (),
        risk="act",
        area="session",
        keywords=("stop listening", "go away", "leave us alone", "mic", "quiet"),
    ),
    ToolSpec(
        "get_now_playing",
        GET_NOW_PLAYING,
        {},
        (),
        risk="read",
        area="session",
        keywords=("now playing", "what is running", "is the pc busy", "session"),
    ),
    ToolSpec(
        "quit_game",
        QUIT_GAME,
        {"appid": {"type": "integer", "description": "appid of the running game"}},
        ("appid",),
        risk="act",
        area="session",
        keywords=("quit", "close game", "exit game", "stop game", "kill"),
    ),
    ToolSpec(
        "nav",
        NAV,
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
        ("target",),
        risk="act",
        area="session",
        keywords=(
            "navigate",
            "show me",
            "open the store page",
            "big picture",
            "downloads page",
            "collection",
        ),
    ),
    ToolSpec(
        "install_game",
        INSTALL_GAME,
        {
            "appid": {
                "type": "integer",
                "description": "appid of an owned, not-yet-installed game",
            }
        },
        ("appid",),
        risk="act",
        area="session",
        keywords=("install", "download game", "not installed", "queue download"),
    ),
]


def known_appids():
    index = library.load()
    ids = {r["appid"] for r in index.get("installed", [])}
    ids.update(int(a) for a in index.get("owned", {}))
    return ids


def impls(ctx: ToolContext):
    dispatch, log = ctx.dispatch, ctx.log
    operations, steam, voice = ctx.operations, ctx.steam, ctx.voice

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

    def session(args):
        action = args.get("action")
        if action == "switch_input":
            r = dispatch.switch_input(str(args.get("input", "")))
        elif action == "end_session":
            r = dispatch.end_session()
        elif action == "start_session":
            r = dispatch.start_session()
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        return {"ok": r.ok, "detail": r.detail}

    def volume(args):
        action = args.get("action")
        if action == "set":
            if "level" not in args:
                return {"ok": False, "error": "set needs level"}
            r = dispatch.volume_set(int(args["level"]))
        elif action == "up":
            r = dispatch.volume_up()
        elif action == "down":
            r = dispatch.volume_down()
        elif action == "mute":
            r = dispatch.mute_toggle()
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        return {"ok": r.ok, "detail": r.detail}

    def stop_listening(args):
        """Acts on the CONVERSATION rather than the room. Not dry-run gated,
        unlike everything in dispatch.py: closing our own mic changes nothing
        on the TV or the PC."""
        if ctx.on_stop_listening is None:
            return {
                "ok": False,
                "error": "there is no open voice session to "
                "close - nothing is listening in the first place",
            }
        ctx.on_stop_listening()
        # end_turn: no second model turn, nothing spoken after this.
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

    return {
        "launch_game": launch_game,
        "session": session,
        "volume": volume,
        "stop_listening": stop_listening,
        "get_now_playing": get_now_playing,
        "quit_game": quit_game,
        "nav": nav,
        "install_game": install_game,
    }
