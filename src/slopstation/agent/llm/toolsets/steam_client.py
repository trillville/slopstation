"""Tools that act on the PC's Steam client through the account session.

The same REST channel install_game uses: Steam's client-comm service, with
the account's own token. Every action re-reads the client's app list and
reports what Steam actually holds, because these calls answer an empty 200
whether or not anything happened.
"""

from __future__ import annotations

from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
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
        # Default: "how far along is the download" is asked from the couch
        # too often to cost a search first.
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
    bind = Bindings(ctx, SPECS)
    dispatch, log, steam = ctx.dispatch, ctx.log, ctx.steam

    def _ready():
        if steam is None or not steam.available():
            return {"ok": False, "error": "the Steam account session isn't enrolled"}
        return None

    @bind
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
        if dry := ctx.preview(f"{action} {appid or 'all downloads'}"):
            return dry
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

    @bind
    def pause_downloads(args):
        return _switch("pause", args)

    @bind
    def resume_downloads(args):
        return _switch("resume", args)

    @bind.destructive
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

        def act():
            try:
                out = steam.uninstall(appid)
            except Exception as e:
                log.error("uninstall_error", appid=appid, err=str(e))
                return {
                    "ok": False,
                    "error": "couldn't reach Steam, so nothing was uninstalled",
                }
            return {**out, "name": name}

        return Plan(
            ("uninstall", appid),
            f"Uninstall {name} from the PC?",
            act,
            f"uninstall {name}",
        )

    return bind.impls()
