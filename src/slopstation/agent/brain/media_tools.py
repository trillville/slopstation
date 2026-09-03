"""The media tools: Radarr/Sonarr lookups, requests and deletes through the
media boundary (tools/media.py). Requests are tracked in the operations ledger;
a delete is confirmed across two turns.
"""

import time
from typing import Any

from slopstation.agent.tools import operations as operations_mod

ASK_TTL_S = 120  # a delete confirmation goes stale

_FIND_MEDIA = """\
Resolve a movie or series title before requesting it. Returns at most five
canonical candidates with year and a TMDB movie id or TVDB series id. Use the
returned id in a request tool only when the intended candidate is clear; ask a
short clarifying question otherwise."""

_MEDIA_LIBRARY = """\
Read what the library already holds for one movie or series - the answer to
'what seasons do I have', 'is <movie> downloaded', and the check before any
deletion. Pass the id returned by find_media. A movie reports available or
not; a series reports have vs aired episode counts per season. Ownership
never comes from conversation memory or the catalog - always call this."""

_REQUEST_MOVIE = """\
Request one movie by a tmdb_id returned by find_media. preset is default,
1080p, or 2160p; omit it unless the user gives a quality preference. This can
start a large download, so call it only for an explicit request and never with
a guessed id."""

_REQUEST_SERIES = """\
Request one series by a tvdb_id returned by find_media. Pass explicit positive
season numbers, or set all_seasons=true only when the user explicitly requests
the whole series or every season. Never omit both scopes. preset is default,
1080p, or 2160p. This can start many large downloads, so call it only for an
explicit request and never with a guessed id. After success, use the returned
acknowledgment as the entire reply without paraphrasing it."""

_DELETE_MEDIA = """\
Cleanly cancel or delete media through Radarr or Sonarr: this erases imported
files and active downloads in that scope and cannot be undone. Resolve the title
with find_media first and pass its catalog id. For a series, pass explicit
positive season numbers, or set all_seasons=true only when the user explicitly
asks to delete the entire series. The first call on a scope deletes nothing and
answers with the title the authority itself holds; put that question to the user
verbatim and call again unchanged only once they have answered yes. A repeat
inside the same turn is always refused, and so is an ask older than two minutes,
but nothing else checks their answer - a no is yours to honour."""

TOOL_DEFS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "find_media",
        _FIND_MEDIA,
        {
            "kind": {"type": "string", "enum": ["movie", "series"]},
            "query": {
                "type": "string",
                "description": "spoken title and optional year",
            },
        },
        ["kind", "query"],
    ),
    (
        "media_library",
        _MEDIA_LIBRARY,
        {
            "kind": {"type": "string", "enum": ["movie", "series"]},
            "catalog_id": {
                "type": "integer",
                "description": "TMDB movie id or TVDB series id returned by find_media",
            },
        },
        ["kind", "catalog_id"],
    ),
    (
        "request_movie",
        _REQUEST_MOVIE,
        {
            "tmdb_id": {"type": "integer", "description": "id returned by find_media"},
            "preset": {"type": "string", "enum": ["default", "1080p", "2160p"]},
        },
        ["tmdb_id"],
    ),
    (
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
        ["tvdb_id"],
    ),
    (
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
        ["kind", "catalog_id"],
    ),
]


def _season_scope(seasons):
    if len(seasons) == 1:
        return f"season {seasons[0]}"
    return "seasons " + ", ".join(str(n) for n in seasons[:-1]) + f" and {seasons[-1]}"


def tool_impls(dispatch, log, operations, media):
    """name -> fn(args: dict) -> dict for the five media tools;
    assistant.tool_impls adds them when a media service is wired in."""

    def find_media(args):
        kind = str(args.get("kind", ""))
        try:
            candidates = media.find(kind, args.get("query", ""))
            return {"ok": True, "kind": kind, "candidates": candidates}
        except Exception as e:
            log.error("tool_error", tool="find_media", err=str(e))
            return {"ok": False, "error": str(e)}

    def media_library(args):
        kind = str(args.get("kind", ""))
        try:
            return {"ok": True, **media.library(kind, args.get("catalog_id"))}
        except Exception as e:
            log.error("tool_error", tool="media_library", err=str(e))
            return {"ok": False, "error": str(e)}

    def _track_media(submission):
        return operations_mod.track(operations, submission, dispatch.utterance.turn)

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
            if dispatch.dry_run:
                scope = "all normal seasons" if all_seasons else _season_scope(seasons)
                detail = f"would request TVDB {tvdb_id}, {scope}, with preset {preset}"
                log("dry_run_would", action=detail)
                return {"ok": True, "dry_run": True, "detail": detail}
            submission = media.request_series(tvdb_id, preset, seasons)
            submission["all_seasons"] = all_seasons
            result = _track_media(submission)
            scope = "all normal seasons" if all_seasons else _season_scope(seasons)
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

    pending_delete: dict[tuple, tuple] = {}  # delete scope -> (turn that asked, when)

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
        if dispatch.dry_run:
            scope = "all seasons" if all_seasons else seasons
            detail = f"would delete {kind} {catalog_id} scope {scope}"
            log("dry_run_would", action=detail)
            return {"ok": True, "dry_run": True, "detail": detail}
        try:
            entry = media.library(kind, catalog_id)
            scope = (kind, catalog_id, tuple(seasons or ()), all_seasons)
            asked_turn, asked_at = pending_delete.get(scope, (None, 0.0))
            if entry["in_library"] and (
                asked_turn in (None, dispatch.utterance.turn)
                or time.time() - asked_at > ASK_TTL_S
            ):
                pending_delete[scope] = (dispatch.utterance.turn, time.time())
                named = (
                    " ".join(
                        str(part) for part in (entry["title"], entry["year"]) if part
                    )
                    or f"{kind} {catalog_id}"
                )
                if all_seasons:
                    named += ", every season"
                elif seasons:
                    named += ", " + _season_scope(seasons)
                log.warn(
                    "tool_refused",
                    tool="delete_media",
                    reason="unconfirmed",
                    catalog_id=catalog_id,
                )
                return {
                    "ok": False,
                    "acknowledgment": f"Delete {named}? That erases the files.",
                }
            pending_delete.pop(scope, None)
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

    return {
        "find_media": find_media,
        "media_library": media_library,
        "request_movie": request_movie,
        "request_series": request_series,
        "delete_media": delete_media,
    }
