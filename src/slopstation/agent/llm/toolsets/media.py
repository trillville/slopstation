"""Tools for movies and series: lookup, requests, and deletion."""

from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
from slopstation.agent.tools import operations as operations_mod

_FIND_MEDIA = """\
Resolve a movie or series title before requesting it. Returns at most five
canonical candidates with year and a TMDB movie id or TVDB series id. Use the
returned id in a request tool only when the intended candidate is clear; ask a
short clarifying question otherwise. Never guess an id: every id a request or
deletion uses comes from this tool."""

_MEDIA_LIBRARY = """\
Read what the library already holds for one movie or series - the answer to
'what seasons do I have', 'is <movie> downloaded', and the check before any
deletion. Pass the id returned by find_media. A movie reports available or
not; a series reports have vs aired episode counts per season. Ownership
never comes from conversation memory or the catalog - always call this. A
request tool skips what is already present, so never re-request media just
because the user says they lack it."""

_REQUEST_MOVIE = """\
Request one movie by a tmdb_id returned by find_media. preset is default,
1080p, or 2160p; a quality preference applies only to that request, so omit
it unless the user gives one. This can start a large download, so call it
only for an explicit request and never with a guessed id."""

_REQUEST_SERIES = """\
Request one series by a tvdb_id returned by find_media. Pass explicit positive
season numbers, or set all_seasons=true only when the user explicitly requests
the whole series or every season. Never omit both scopes: a bare series
request is ambiguous, so ask which season, or whether they want all seasons,
and call nothing until they answer. preset is default, 1080p, or 2160p, and
applies only to that request. This can start many large downloads, so call
it only for an explicit request and never with a guessed id. After success,
use the returned acknowledgment as the entire reply without paraphrasing it."""

_DELETE_MEDIA = """\
Cleanly cancel or delete media through Radarr or Sonarr: this erases imported
files and active downloads in that scope and cannot be undone. Resolve the title
with find_media first and pass its catalog id. For a series, pass explicit
positive season numbers, or set all_seasons=true only when the user explicitly
asks to delete the entire series; every other season is preserved. The first
call on a scope deletes nothing and answers with the title the authority itself
holds; put that question to the user verbatim and call again unchanged only
once they have answered yes. A repeat inside the same turn is always refused,
and so is an ask older than ten minutes, but nothing else checks their answer
- a no is yours to honour."""

SPECS = [
    ToolSpec(
        "find_media",
        _FIND_MEDIA,
        {
            "kind": {"type": "string", "enum": ["movie", "series"]},
            "query": {
                "type": "string",
                "description": "spoken title and optional year",
            },
        },
        ("kind", "query"),
        risk="read",
        area="media",
        keywords=("movie", "series", "show", "tmdb", "tvdb", "which one", "lookup"),
        needs=("media",),
    ),
    ToolSpec(
        "media_library",
        _MEDIA_LIBRARY,
        {
            "kind": {"type": "string", "enum": ["movie", "series"]},
            "catalog_id": {
                "type": "integer",
                "description": "TMDB movie id or TVDB series id returned by find_media",
            },
        },
        ("kind", "catalog_id"),
        risk="read",
        area="media",
        keywords=("do i have", "downloaded", "which seasons", "library", "available"),
        needs=("media",),
    ),
    ToolSpec(
        "request_movie",
        _REQUEST_MOVIE,
        {
            "tmdb_id": {"type": "integer", "description": "id returned by find_media"},
            "preset": {"type": "string", "enum": ["default", "1080p", "2160p"]},
        },
        ("tmdb_id",),
        risk="act",
        area="media",
        keywords=("download movie", "get the movie", "request", "radarr", "4k"),
        needs=("media",),
    ),
    ToolSpec(
        "request_series",
        _REQUEST_SERIES,
        {
            "tvdb_id": {"type": "integer", "description": "id returned by find_media"},
            "preset": {"type": "string", "enum": ["default", "1080p", "2160p"]},
            "seasons": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "positive season numbers explicitly requested",
            },
            "all_seasons": {
                "type": "boolean",
                "description": "true only for an explicit whole-series request",
            },
        },
        ("tvdb_id",),
        risk="act",
        area="media",
        keywords=(
            "download show",
            "get season",
            "request series",
            "sonarr",
            "episodes",
        ),
        needs=("media",),
    ),
    ToolSpec(
        "delete_media",
        _DELETE_MEDIA,
        {
            "kind": {"type": "string", "enum": ["movie", "series"]},
            "catalog_id": {
                "type": "integer",
                "description": "TMDB movie id or TVDB series id returned by find_media",
            },
            "seasons": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "series seasons to delete; preserve every other season",
            },
            "all_seasons": {
                "type": "boolean",
                "description": "true only for an explicit whole-series deletion",
            },
        },
        ("kind", "catalog_id"),
        risk="destructive",
        area="media",
        keywords=("delete", "remove", "erase", "cancel download", "free space"),
        default=False,
        needs=("media",),
    ),
]


def _season_scope(seasons):
    if len(seasons) == 1:
        return f"season {seasons[0]}"
    return "seasons " + ", ".join(str(n) for n in seasons[:-1]) + f" and {seasons[-1]}"


def impls(ctx: ToolContext):
    """name -> fn(args: dict) -> dict for the five media tools."""
    bind = Bindings(ctx, SPECS)
    log, operations, media = ctx.log, ctx.operations, ctx.media

    @bind
    def find_media(args):
        kind = str(args.get("kind", ""))
        try:
            candidates = media.find(kind, args.get("query", ""))
            return {"ok": True, "kind": kind, "candidates": candidates}
        except Exception as e:
            log.error("tool_error", tool="find_media", err=str(e))
            return {"ok": False, "error": str(e)}

    @bind
    def media_library(args):
        kind = str(args.get("kind", ""))
        try:
            return {"ok": True, **media.library(kind, args.get("catalog_id"))}
        except Exception as e:
            log.error("tool_error", tool="media_library", err=str(e))
            return {"ok": False, "error": str(e)}

    def _track_media(submission):
        return operations_mod.track(operations, submission, ctx.turn())

    @bind
    def request_movie(args):
        try:
            tmdb_id = int(args.get("tmdb_id", 0))
            preset = args.get("preset", "default")
            if tmdb_id <= 0:
                return {"ok": False, "error": "tmdb_id must be positive"}
            if dry := ctx.preview(f"request TMDB {tmdb_id} with preset {preset}"):
                return dry
            return _track_media(media.request_movie(tmdb_id, preset))
        except Exception as e:
            log.error("tool_error", tool="request_movie", err=str(e))
            return {"ok": False, "error": str(e)}

    @bind
    def request_series(args):
        try:
            tvdb_id = int(args.get("tvdb_id", 0))
            preset = args.get("preset", "default")
            if tvdb_id <= 0:
                return {"ok": False, "error": "tvdb_id must be positive"}
            seasons = args.get("seasons")
            all_seasons = args.get("all_seasons", False)
            if not isinstance(all_seasons, bool):
                return {"ok": False, "error": "all_seasons must be boolean"}
            if seasons is not None and all_seasons:
                return {
                    "ok": False,
                    "error": "choose explicit seasons or all_seasons, not both",
                }
            if seasons is None and not all_seasons:
                return {
                    "ok": False,
                    "error": "series request needs explicit scope",
                    "clarification": "Which season would you like, or "
                    "should I download all seasons?",
                }
            if seasons is not None:
                if not isinstance(seasons, list) or not seasons:
                    return {"ok": False, "error": "seasons must be a non-empty list"}
                if any(
                    isinstance(n, bool) or not isinstance(n, int) or n <= 0
                    for n in seasons
                ):
                    return {
                        "ok": False,
                        "error": "season numbers must be positive integers",
                    }
                seasons = sorted(set(seasons))
            scope = "all normal seasons" if all_seasons else _season_scope(seasons)
            if dry := ctx.preview(
                f"request TVDB {tvdb_id}, {scope}, with preset {preset}"
            ):
                return dry
            submission = media.request_series(tvdb_id, preset, seasons)
            submission["all_seasons"] = all_seasons
            result = _track_media(submission)
            quality = (
                "using the default quality profile"
                if result.get("preset") == "default"
                else f"in {result.get('preset')}"
            )
            if result.get("already_available"):
                acknowledgment = (
                    f"{result['title']}, {scope}, {quality} is already available."
                )
            else:
                acknowledgment = (
                    f"Requested {result['title']}, {scope}, "
                    f"{quality}. Sonarr is searching in the "
                    "background."
                )
            return {**result, "acknowledgment": acknowledgment}
        except Exception as e:
            log.error("tool_error", tool="request_series", err=str(e))
            return {"ok": False, "error": str(e)}

    @bind.destructive
    def delete_media(args):
        try:
            kind = str(args.get("kind", ""))
            catalog_id = int(args.get("catalog_id", 0) or 0)
            seasons = args.get("seasons")
            all_seasons = bool(args.get("all_seasons", False))
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "catalog_id must be an integer"}
        if kind not in ("movie", "series"):
            return {"ok": False, "error": f"unknown media kind {kind}"}
        if catalog_id <= 0:
            return {"ok": False, "error": "catalog_id must be positive"}
        if kind == "series" and seasons is None and not all_seasons:
            return {
                "ok": False,
                "error": "name seasons or explicitly request all seasons",
            }
        if seasons is not None:
            if (
                not isinstance(seasons, list)
                or not seasons
                or any(
                    isinstance(n, bool) or not isinstance(n, int) or n <= 0
                    for n in seasons
                )
            ):
                return {
                    "ok": False,
                    "error": "season numbers must be positive integers",
                }
            seasons = sorted(set(seasons))
        try:
            entry = media.library(kind, catalog_id)
        except Exception as e:
            log.error("tool_error", tool="delete_media", err=str(e))
            return {"ok": False, "error": str(e)}
        named = (
            " ".join(str(part) for part in (entry["title"], entry["year"]) if part)
            or f"{kind} {catalog_id}"
        )
        if all_seasons:
            named += ", every season"
        elif seasons:
            named += ", " + _season_scope(seasons)

        def act():
            try:
                covered, command_ids = operations_mod.covered_by_delete(
                    operations, kind, catalog_id, seasons, all_seasons
                )
                if kind == "movie":
                    result = media.delete_movie(catalog_id, command_ids)
                else:
                    result = media.delete_series(
                        catalog_id,
                        seasons=seasons,
                        all_seasons=all_seasons,
                        command_ids=command_ids,
                    )
                return operations_mod.record_deleted(operations, covered, result)
            except Exception as e:
                log.error("tool_error", tool="delete_media", err=str(e))
                return {"ok": False, "error": str(e)}

        if not entry["in_library"]:
            # Nothing on disk to lose: the app never held it, or let go.
            return act()
        return Plan(
            (kind, catalog_id, tuple(seasons or ()), all_seasons),
            f"Delete {named}? That erases the files.",
            act,
            f"delete {named}",
        )

    return bind.impls()
