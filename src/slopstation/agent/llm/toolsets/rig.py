"""Tools for the rig itself: the session, the TV, the mic, and Big Picture."""

import json
import subprocess
import urllib.parse

from slopstation import gamepc, sessionlock
from slopstation.agent.llm.registry import Bindings, ToolContext, ToolSpec
from slopstation.agent.tools import library

STORE_SEARCH = "https://store.steampowered.com/search/?term="

LAUNCH_GAME = """\
Launch a game from the catalog by appid. Starts a session automatically if
none is running - never call start_session first."""

SESSION = """\
Control the session: end_session, start_session, switch_input (with input
name; the valid names are in the system prompt). Ending the session and
switching input both interrupt what is on the TV, so never take either as a
guess. switch_input only changes which input the TV shows; putting the PC's
DESKTOP on the TV, or back on the monitor, is the display tool, not this.
nav and launch_game start a session themselves when none is live, so
start_session is for 'start a session' said plainly. start_session returns
while the session is still coming up - don't call nav in the same turn; say
it's starting and let the user ask again."""

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
Navigate the Big Picture UI on the TV. With no session live this starts one
and opens the page once Big Picture is up, about fifteen seconds later: say
the page is coming, and never call start_session first. Pages that need
nothing else: 'downloads', 'library', 'store', 'friends', 'settings',
'screenshots', 'wishlist', 'news'. Pages for one game, by appid: 'game_page'
(an OWNED game's library page with its Play button - 'show me <game>'),
'store_page' (any game's store page, owned or not - the way to put a game
the user wants to BUY or INSTALL on the TV so they can press the button),
'dlc' (its DLC list), 'community_hub' (reviews, discussions, guides),
'workshop', 'news' (its patch notes when an appid is given), 'verify_files'
(check its files). 'collection' shows one of the user's own library
collections by name (pass it in `collection` - on a miss the result lists
the real ones, so use those rather than guessing again). 'search' shows
store results for `query`. 'web' opens any page on store.steampowered.com
or steamcommunity.com given in `url` - sale events, curators, a profile."""

TV_STATUS = """\
The TV as it is right now: power state, volume and mute, read back over the
network. Read only; the answer to 'is the TV on' and 'how loud is it'."""

PC_STATUS = """\
The gaming PC as it is right now: reachable or asleep, whether a session is
live and its turn, what Steam reports running, whether Steam is signed in
online, and the free space on each Steam library drive - the answer to 'can
I install that' and 'is the PC awake'. Reaching an asleep PC takes a few
seconds to time out."""

PC_POWER = """\
Wake the gaming PC (a magic packet; it takes a minute to come up, and
start_session does this itself) or put it to sleep. Sleep is refused while a
session is live, a game is running, or someone is signed in at the desk, so
it cannot end what is on the TV or under someone's hands."""

DISPLAY = """\
Put the PC's DESKTOP on the TV, or back on the desk monitor, with NO
session: 'tv' switches the TV to the PC and moves the desktop there, for
using the PC on the TV without Steam Big Picture or to fix a display stuck
the wrong way; 'monitor' puts it back on the desk. Mouse and keyboard only -
the controller is not part of this - and nothing moves it back on its own:
say so. This is what 'desktop' or 'monitor' means when no session is live.
Refused while a session is live: then 'back to the monitor' and 'back to
the office' mean end_session, which restores the monitor itself."""

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
        keywords=(
            "now playing",
            "what is running",
            "currently running",
            "what game is on",
            "is the pc busy",
            "session",
        ),
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
                    "friends",
                    "settings",
                    "screenshots",
                    "wishlist",
                    "news",
                    "game_page",
                    "store_page",
                    "dlc",
                    "community_hub",
                    "workshop",
                    "verify_files",
                    "collection",
                    "search",
                    "web",
                ],
            },
            "appid": {
                "type": "integer",
                "description": "for game_page (must be owned), store_page, dlc, "
                "community_hub, workshop, verify_files (any Steam appid), and "
                "optionally news",
            },
            "collection": {
                "type": "string",
                "description": "collection name, for target=collection",
            },
            "query": {
                "type": "string",
                "description": "store search words, for target=search",
            },
            "url": {
                "type": "string",
                "description": "a store.steampowered.com or steamcommunity.com "
                "page, for target=web",
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
            "wishlist page",
            "friends list",
            "settings",
            "screenshots",
            "dlc",
            "workshop",
            "community hub",
            "verify files",
            "search the store on the tv",
            "open a page",
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


SPECS += [
    ToolSpec(
        "tv_status",
        TV_STATUS,
        {},
        (),
        risk="read",
        area="session",
        keywords=(
            "is the tv on",
            "tv status",
            "how loud",
            "current volume",
            "is it muted",
        ),
        default=False,
    ),
    ToolSpec(
        "pc_status",
        PC_STATUS,
        {},
        (),
        risk="read",
        area="session",
        keywords=(
            "is the pc awake",
            "pc status",
            "steam library free space",
            "is steam online",
            "room to install",
        ),
        default=False,
    ),
    ToolSpec(
        "display",
        DISPLAY,
        {"target": {"type": "string", "enum": ["tv", "monitor"]}},
        ("target",),
        risk="act",
        area="session",
        keywords=(
            "desktop",
            "monitor",
            "desktop on the tv",
            "put the pc on the tv",
            "display profile",
            "back on the monitor",
            "wrong screen",
            "display stuck",
            "switch the display",
            "without big picture",
        ),
        # Default: with only `session` loaded, "put the desktop on the TV"
        # became an input switch that started a session (2026-09-06 logs).
    ),
    ToolSpec(
        "pc_power",
        PC_POWER,
        {"action": {"type": "string", "enum": ["wake", "sleep"]}},
        ("action",),
        risk="act",
        area="session",
        keywords=(
            "wake the pc",
            "sleep the pc",
            "pc to sleep",
            "put the pc",
            "turn off the pc",
            "power",
            "suspend",
        ),
        default=False,
    ),
]


def known_appids():
    index = library.load()
    ids = {r["appid"] for r in index.get("installed", [])}
    ids.update(int(a) for a in index.get("owned", {}))
    return ids


def impls(ctx: ToolContext):
    bind = Bindings(ctx, SPECS)
    dispatch, log = ctx.dispatch, ctx.log
    operations, steam, voice = ctx.operations, ctx.steam, ctx.voice

    def _unknown(tool, appid):
        """The refusal for an appid outside the catalog, else None."""
        if appid in known_appids():
            return None
        log.warn("tool_refused", tool=tool, reason="unknown_appid", appid=appid)
        return {"ok": False, "error": f"appid {appid} is not in the catalog"}

    @bind
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

    @bind
    def quit_game(args):
        appid = int(args.get("appid", 0))
        if refused := _unknown("quit_game", appid):
            return refused
        r = dispatch.quit_game(appid)
        return {"ok": r.ok, "detail": r.detail}

    @bind
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
        if dry := ctx.preview(f"start the download for appid {appid}"):
            return dry
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
        # With no session, nav starts one and the page comes up with it; the
        # receipt has to say so rather than claim the page is on the TV now.
        starting = not sessionlock.active()
        r = dispatch.nav("details", appid)
        if not r.ok:
            return {"ok": False, "error": r.detail}
        if starting:
            return {
                "ok": True,
                "detail": f"{r.detail} - then press Install and the download starts",
            }
        return {
            "ok": True,
            "detail": "it's on the TV now - press Install and the download starts",
        }

    @bind
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
        elif target in ("dlc", "community_hub", "workshop", "verify_files"):
            # Any Steam appid: these pages exist for games the user does not
            # own too, and a DLC list is one way to put a purchase on the TV.
            appid = int(appid or 0)
            if appid <= 0:
                return {"ok": False, "error": "I need the game's appid"}
            kind = {"community_hub": "hub", "verify_files": "validate"}.get(
                target, target
            )
            r = dispatch.nav(kind, appid)
        elif target == "news":
            appid = int(appid or 0)
            r = dispatch.nav("news", appid if appid > 0 else None)
        elif target == "search":
            query = str(args.get("query") or "").strip()
            if not query:
                return {"ok": False, "error": "search needs the words to search for"}
            # Trim until the encoded URL fits the PC's allowlist length.
            words = query[:120]
            url = STORE_SEARCH + urllib.parse.quote_plus(words)
            while words and not gamepc.NAV_URL_RE.fullmatch(url):
                words = words[:-1]
                url = STORE_SEARCH + urllib.parse.quote_plus(words)
            if not words:
                return {"ok": False, "error": "those search words cannot be encoded"}
            r = dispatch.nav("url", url)
        elif target == "web":
            url = str(args.get("url") or "").strip()
            if not gamepc.NAV_URL_RE.fullmatch(url):
                return {
                    "ok": False,
                    "error": "web opens store.steampowered.com or "
                    "steamcommunity.com pages only, as a plain https URL",
                }
            r = dispatch.nav("url", url)
        elif target in (
            "downloads",
            "library",
            "store",
            "friends",
            "settings",
            "screenshots",
            "wishlist",
        ):
            r = dispatch.nav(target)
        else:
            return {"ok": False, "error": f"unknown nav target {target}"}
        return {"ok": r.ok, "detail": r.detail}

    @bind
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

    @bind
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

    @bind
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

    @bind
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

    @bind
    def tv_status(args):
        out: dict = {"ok": True}
        for key, read in (
            ("power", dispatch.tv.power_state),
            ("volume", dispatch.tv.volume),
            ("muted", dispatch.tv.muted),
        ):
            try:
                out[key] = read()
            except Exception as e:
                out[key] = None
                out.setdefault("errors", []).append(f"{key}: {e}")
        return out

    @bind
    def pc_status(args):
        out: dict = {"ok": True, "session_active": sessionlock.active()}
        try:
            status = gamepc.status()
            out["reachable"] = True
            out["ready"] = status != "NOTREADY"
            out["session_turn"] = status if status != "NOTREADY" else None
        except subprocess.CalledProcessError as e:
            if e.returncode == 255:
                # ssh's own exit code: no connection.
                out["reachable"] = False
                out["detail"] = "the PC did not answer over SSH; it is asleep or off"
                return out
            out["reachable"] = True
            out["detail"] = (
                f"the PC answered but refused the status verb (exit {e.returncode}) "
                "- a version skew; run the doctor"
            )
            return out
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            out["reachable"] = False
            out["detail"] = (
                "the PC did not answer over SSH in time; it is asleep or off"
            )
            return out
        except Exception as e:
            out["reachable"] = True
            out["detail"] = f"the PC answered but its status was unreadable: {e}"
            return out
        try:
            playing = gamepc.playing()
            appid = int(playing) if playing.isdigit() else 0
            out["running"] = (
                {"appid": appid, "name": library.installed_name(appid)}
                if appid
                else None
            )
        except Exception as e:
            out["running_error"] = str(e)
        try:
            rows = json.loads(gamepc.disk() or "[]")
            drives: dict[str, dict] = {}
            for r in rows:
                if isinstance(r, dict) and r.get("drive"):
                    # Two library roots on one drive are one drive.
                    drives.setdefault(
                        str(r["drive"]).lower(),
                        {
                            "drive": r.get("drive"),
                            "free_gb": round(int(r.get("free", 0) or 0) / 1024**3, 1),
                            "total_gb": round(int(r.get("total", 0) or 0) / 1024**3, 1),
                        },
                    )
            out["steam_drives"] = list(drives.values())
        except Exception as e:
            out["steam_drives_error"] = str(e)
        if steam is not None and steam.available():
            try:
                out["steam_online"] = steam.client_online()
            except Exception as e:
                out["steam_online_error"] = str(e)
        return out

    @bind
    def display(args):
        r = dispatch.display(str(args.get("target") or ""))
        return {"ok": r.ok, "detail": r.detail}

    @bind
    def pc_power(args):
        action = str(args.get("action") or "")
        if action not in ("wake", "sleep"):
            return {"ok": False, "error": "action must be wake or sleep"}
        if action == "sleep" and sessionlock.active():
            return {
                "ok": False,
                "error": "a session is live - end it first, or the TV goes dark mid-game",
            }
        if dry := ctx.preview(f"{action} the PC"):
            return dry
        try:
            if action == "wake":
                from slopstation import couch

                couch.wol()
                return {"ok": True, "detail": "wake packet sent - give it a minute"}
            out = gamepc.sleep(dispatch.utterance.turn)
        except Exception as e:
            return {"ok": False, "error": f"couldn't reach the PC ({e})"}
        if out == "OK":
            return {"ok": True, "detail": "the PC is going to sleep"}
        if out.startswith("BUSY"):
            return {"ok": False, "error": "the PC refused: a session or a game is live"}
        return {"ok": False, "error": f"the PC answered {out}"}

    return bind.impls()
