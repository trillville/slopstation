"""Tools that act on the PC's Steam client through the account session.

The same REST channel install_game uses: Steam's client-comm service, with
the account's own token. Every action re-reads the client's app list and
reports what Steam actually holds, because these calls answer an empty 200
whether or not anything happened.
"""

from __future__ import annotations

from slopstation.agent.llm.registry import ToolContext, ToolSpec
from slopstation.agent.tools import library

DOWNLOAD_STATUS = """\
Steam's own download queue on the PC: each changing game with its percent,
paused flag, queue position and phase (downloading, queued, paused,
finalizing). Describe finalizing as finalizing, never as a download. For
Slopstation's tracked work (media requests too) use list_operations."""

PAUSE_DOWNLOADS = """\
Pause Steam downloads on the PC: one game by appid, or with no appid the
client's global download switch, which stops everything. Reports the paused
flags Steam holds afterwards, so say what actually happened. Harmless and
reversible."""

RESUME_DOWNLOADS = """\
Resume Steam downloads on the PC: one game by appid, or with no appid the
client's global download switch. The switch does not un-pause a game that
was paused on its own; pass that game's appid. Reports the paused flags
Steam holds afterwards."""

UNINSTALL_GAME = """\
Uninstall a game from the PC to free its disk space. Refused for the game
that is running. This erases the install and cannot be undone: the first call
answers with the title, say it back and call again unchanged only once the
user has said yes. Owned games can be reinstalled later with install_game."""

SPECS = [
    ToolSpec(
        "download_status",
        DOWNLOAD_STATUS,
        {},
        (),
        risk="read",
        area="steam",
        keywords=(
            "steam download",
            "how far along is the download",
            "download progress",
            "steam queue",
            "is it still downloading",
        ),
        default=False,
        needs=("steam_account",),
    ),
    ToolSpec(
        "pause_downloads",
        PAUSE_DOWNLOADS,
        {"appid": {"type": "integer", "description": "one game; omit for all"}},
        (),
        risk="act",
        area="steam",
        keywords=(
            "pause steam download",
            "pause the download",
            "stop downloading",
            "pause all downloads",
        ),
        default=False,
        needs=("steam_account",),
    ),
    ToolSpec(
        "resume_downloads",
        RESUME_DOWNLOADS,
        {"appid": {"type": "integer", "description": "one game; omit for all"}},
        (),
        risk="act",
        area="steam",
        keywords=(
            "resume steam download",
            "continue the download",
            "unpause download",
            "resume all downloads",
        ),
        default=False,
        needs=("steam_account",),
    ),
    ToolSpec(
        "uninstall_game",
        UNINSTALL_GAME,
        {"appid": {"type": "integer", "description": "appid of an installed game"}},
        ("appid",),
        risk="destructive",
        area="steam",
        keywords=(
            "uninstall",
            "remove the game",
            "free up space on the pc",
            "delete game install",
        ),
        default=False,
        needs=("steam_account",),
    ),
]


def impls(ctx: ToolContext):
    dispatch, log, steam = ctx.dispatch, ctx.log, ctx.steam

    def _ready():
        if steam is None or not steam.available():
            return {"ok": False, "error": "the Steam account session isn't enrolled"}
        return None

    def download_status(args):
        if err := _ready():
            return err
        try:
            rows = steam.download_status()
        except Exception as e:
            log.error("download_status_error", err=str(e))
            return {
                "ok": False,
                "error": "couldn't reach Steam for the download status just now",
            }
        return {"ok": True, "count": len(rows), "downloads": rows}

    def _switch(action, args):
        if err := _ready():
            return err
        appid = args.get("appid")
        try:
            appid = int(appid) if appid is not None else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "appid must be an integer"}
        if dispatch.dry_run:
            log("dry_run_would", action=f"{action} downloads {appid or 'all'}")
            return {
                "ok": True,
                "dry_run": True,
                "detail": f"would {action} {appid or 'all downloads'}",
            }
        try:
            if appid is None:
                return steam.enable_downloads(action == "resume")
            out = steam.set_update_state(appid, action)
        except Exception as e:
            log.error("download_switch_error", action=action, err=str(e))
            return {"ok": False, "error": f"couldn't reach Steam to {action}"}
        if out.get("ok") and out.get("verified") is False:
            out["detail"] = (
                "Steam accepted the request but its app list still shows the "
                f"download as {'running' if action == 'pause' else 'paused'} - "
                "say so plainly rather than claiming it worked"
            )
        return out

    def pause_downloads(args):
        return _switch("pause", args)

    def resume_downloads(args):
        return _switch("resume", args)

    def uninstall_game(args):
        if err := _ready():
            return err
        try:
            appid = int(args.get("appid", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "appid must be an integer"}
        name = library.installed_name(appid)
        if name is None:
            return {"ok": False, "error": "that game is not installed on the PC"}
        playing = dispatch.now_playing()
        if playing.ok and str(playing.detail) == str(appid):
            return {"ok": False, "error": f"{name} is running - quit it first"}
        if dispatch.dry_run:
            log("dry_run_would", action=f"uninstall {appid}")
            return {"ok": True, "dry_run": True, "detail": f"would uninstall {name}"}
        if not ctx.gate.confirmed(("uninstall", appid), dispatch.utterance.turn):
            log.warn(
                "tool_refused", tool="uninstall_game", reason="unconfirmed", appid=appid
            )
            return {"ok": False, "acknowledgment": f"Uninstall {name} from the PC?"}
        try:
            out = steam.uninstall(appid)
        except Exception as e:
            log.error("uninstall_error", appid=appid, err=str(e))
            return {
                "ok": False,
                "error": "couldn't reach Steam, so nothing was uninstalled",
            }
        if out.get("ok"):
            ctx.gate.done(("uninstall", appid))
        return {**out, "name": name}

    return {
        "download_status": download_status,
        "pause_downloads": pause_downloads,
        "resume_downloads": resume_downloads,
        "uninstall_game": uninstall_game,
    }
