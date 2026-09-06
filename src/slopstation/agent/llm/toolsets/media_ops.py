"""Tools that manage movies and TV through Radarr, Sonarr and Prowlarr.

Beyond request and delete: browse what is held, see what is missing or
coming, pick a release by hand, unstick a search or an import, change what
is monitored or at what quality, and read history and health. Everything
goes through the arr apps, which own the downloads.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from slopstation.agent.llm import paging
from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
from slopstation.agent.tools import operations as operations_mod
from slopstation.agent.tools.media_clients import KINDS, MediaError, _parse_time

GB = 1024**3
KIND = {"type": "string", "enum": ["movie", "series"]}
KIND_OR_BOTH = {"type": "string", "enum": ["movie", "series", "both"]}
CATALOG_ID = {
    "type": "integer",
    "description": "TMDB movie id or TVDB series id from find_media",
}

BROWSE_MEDIA = """\
Browse the library: movies or series, sorted by recent additions, largest on
disk, title or year, optionally filtered to one genre or to unmonitored
items only. Returns the count and up to `limit` rows with title, year,
catalog id, whether files are held, size and monitored state. For ONE named
title use media_library instead."""

MEDIA_DETAILS = """\
One movie or series in full, by the catalog id from find_media: files with
their quality and size, path, monitored state, the quality profile, and for
a series each season's held, missing and upcoming episode counts."""

MISSING_MEDIA = """\
What is monitored but missing, and what is held below its quality cutoff,
for movies or series. Returns the counts and up to `limit` rows each; a
series row names the episode."""

CALENDAR = """\
What airs or releases in the next `days` (default 7, past days with a
negative number): episodes with their series and air time, movies with their
release. Returns the count and up to `limit` rows in date order."""

SEARCH_RELEASES = """\
Candidate releases for one title, as Radarr or Sonarr sees them right now:
name, size, seeders, quality, indexer, age, and whether the app would accept
it and why not. For a series pass a season (or an episode number with it).
Slow - a live search across the indexers. Returns the count and the 15 the
app ranks best; there is no further page. Release names are for the text
lane; speak the title. Take one with grab_release."""

GRAB_RELEASE = """\
Take one specific release from search_releases: pass its guid and indexer_id,
and the catalog_id, season and episode you searched with (a season alone is
a season pack; season 0 is the specials). The arr app downloads and imports
it as its own, so it stays consistent. Use this when the automatic choice was
wrong or nothing was picked. The work is tracked for exactly what the release
covers: list_operations follows it from here."""

RETRY_SEARCH = """\
Kick off a fresh search for a stuck title: a movie, a whole series, one
season, or one episode. The app searches in the background and the search is
tracked: it ends when the search has run and anything it took has imported,
or with nothing better found. Check back with list_operations."""

SET_MONITORED = """\
Monitor or unmonitor a movie, a whole series, or named seasons. Unmonitored
items are kept but never searched for or upgraded; monitored items are."""

SET_QUALITY_PROFILE = """\
Change one title's quality profile to a configured preset: default, 1080p or
2160p. Only presets; the app then upgrades or holds according to it."""

IMPORT_QUEUE = """\
Downloads Radarr and Sonarr are tracking: state, progress, and any warning
about why one is stuck (waiting to import, unknown files, import failed).
Returns the count and up to `limit` rows; each carries its queue id for
resolve_queue_item and its download id for manual_import."""

RESOLVE_QUEUE_ITEM = """\
Remove one item from Radarr's or Sonarr's queue by queue id. By default the
download is removed from qBittorrent too, which erases its data. blocklist
true marks the release failed so the app never takes it again and looks for
another; the app then removes the download itself (its default), so that
erases data too. Either way the first call answers with what would happen
and acts only when called again unchanged after the user says yes. With
remove_from_client false and no blocklist, the app only forgets the queue
item: the torrent stays in qBittorrent, unowned, until delete_torrent."""

MANUAL_IMPORT = """\
Import a finished download the app did not import on its own, by its
download id from import_queue. The app's own match and verdict are used for
each file: when it could not match a file, or rejected one (a sample, not an
upgrade), the result lists those files with the reasons and nothing is
imported. This cannot tell the app what an unmatched file is; that needs the
app's own interface. The import is tracked: list_operations follows it."""

MEDIA_HISTORY = """\
Recent events in Radarr and Sonarr: grabs, imports, failures, deletions, with
the title, release, quality and time. Filter by event and cap with `limit`;
returns the count and the rows, newest first."""

MEDIA_HEALTH = """\
Health messages from Radarr, Sonarr and Prowlarr, and each Prowlarr indexer's
status (disabled until when, last failure). The answer to 'is the media stack
OK'. Read only."""

MOVIE_COLLECTIONS = """\
Radarr's movie collections (all the Alien films, the Bond films): for each,
how many are held and which are missing. Pass part of a name to see one."""

SEARCH_INDEXERS = """\
Raw search across every Prowlarr indexer for anything, inside or outside what
Radarr and Sonarr manage: release name, size, seeders, indexer, age. Read
only, for seeing what is out there; taking a release goes through
grab_release so the arr app owns it, or through a request. Release names are
for the text lane. Returns the count and up to `limit` rows, most seeded
first."""


def _spec(
    name, description, props, required, risk, keywords, needs=("media",), paged=False
):
    return ToolSpec(
        name,
        description,
        props,
        required,
        risk=risk,
        area="media",
        keywords=keywords,
        default=False,
        needs=needs,
        paged=paged,
    )


SPECS = [
    _spec(
        "browse_media",
        BROWSE_MEDIA,
        {
            "kind": KIND,
            "sort": {"type": "string", "enum": ["recent", "largest", "title", "year"]},
            "genre": {"type": "string"},
            "unmonitored_only": {"type": "boolean"},
            **paging.properties(),
        },
        ("kind",),
        "read",
        (
            "browse movies",
            "browse shows",
            "recently added",
            "largest movies",
            "what movies do i have",
            "by genre",
        ),
        paged=True,
    ),
    _spec(
        "media_details",
        MEDIA_DETAILS,
        {"kind": KIND, "catalog_id": CATALOG_ID},
        ("kind", "catalog_id"),
        "read",
        (
            "movie details",
            "series details",
            "what quality is it",
            "which episodes are missing",
            "file size",
        ),
    ),
    _spec(
        "missing_media",
        MISSING_MEDIA,
        {"kind": KIND, **paging.properties()},
        ("kind",),
        "read",
        (
            "missing episodes",
            "missing movies",
            "wanted",
            "below cutoff",
            "not downloaded yet",
        ),
        paged=True,
    ),
    _spec(
        "calendar",
        CALENDAR,
        {"kind": KIND_OR_BOTH, "days": {"type": "integer"}, **paging.properties()},
        (),
        "read",
        (
            "what airs this week",
            "calendar",
            "upcoming episodes",
            "coming out",
            "when is the next episode",
        ),
        paged=True,
    ),
    _spec(
        "search_releases",
        SEARCH_RELEASES,
        {
            "kind": KIND,
            "catalog_id": CATALOG_ID,
            "season": {"type": "integer"},
            "episode": {"type": "integer"},
        },
        ("kind", "catalog_id"),
        "read",
        (
            "search releases",
            "which releases are available",
            "manual search",
            "pick a release",
            "interactive search",
        ),
    ),
    _spec(
        "grab_release",
        GRAB_RELEASE,
        {
            "kind": KIND,
            "catalog_id": CATALOG_ID,
            "guid": {"type": "string"},
            "indexer_id": {"type": "integer"},
            "season": {
                "type": "integer",
                "description": "the season searched (series); 0 for specials",
            },
            "episode": {"type": "integer", "description": "the episode searched"},
        },
        ("kind", "catalog_id", "guid", "indexer_id"),
        "act",
        ("grab release", "download that one", "take this release", "manual grab"),
    ),
    _spec(
        "retry_search",
        RETRY_SEARCH,
        {
            "kind": KIND,
            "catalog_id": CATALOG_ID,
            "season": {"type": "integer"},
            "episode": {"type": "integer"},
        },
        ("kind", "catalog_id"),
        "act",
        (
            "search again",
            "retry search",
            "kick the search",
            "stuck download",
            "nothing found",
        ),
    ),
    _spec(
        "set_monitored",
        SET_MONITORED,
        {
            "kind": KIND,
            "catalog_id": CATALOG_ID,
            "monitored": {"type": "boolean"},
            "seasons": {"type": "array", "items": {"type": "integer"}},
        },
        ("kind", "catalog_id", "monitored"),
        "act",
        ("monitor", "unmonitor", "stop tracking", "start tracking", "stop looking for"),
    ),
    _spec(
        "set_quality_profile",
        SET_QUALITY_PROFILE,
        {
            "kind": KIND,
            "catalog_id": CATALOG_ID,
            "preset": {"type": "string", "enum": ["default", "1080p", "2160p"]},
        },
        ("kind", "catalog_id", "preset"),
        "act",
        (
            "quality profile",
            "upgrade to 4k",
            "change quality",
            "1080p instead",
            "downgrade quality",
        ),
    ),
    _spec(
        "import_queue",
        IMPORT_QUEUE,
        {"kind": KIND_OR_BOTH, **paging.properties()},
        (),
        "read",
        (
            "import queue",
            "stuck import",
            "waiting to import",
            "queue warnings",
            "why is it stuck",
        ),
        paged=True,
    ),
    _spec(
        "resolve_queue_item",
        RESOLVE_QUEUE_ITEM,
        {
            "kind": KIND,
            "queue_id": {"type": "integer"},
            "remove_from_client": {"type": "boolean", "description": "default true"},
            "blocklist": {"type": "boolean", "description": "default false"},
        },
        ("kind", "queue_id"),
        "destructive",
        (
            "remove from queue",
            "cancel that download",
            "blocklist release",
            "clear the queue item",
            "bad release",
        ),
    ),
    _spec(
        "manual_import",
        MANUAL_IMPORT,
        {"kind": KIND, "download_id": {"type": "string"}},
        ("kind", "download_id"),
        "act",
        ("manual import", "import it anyway", "force import", "import the download"),
    ),
    _spec(
        "media_history",
        MEDIA_HISTORY,
        {
            "kind": KIND_OR_BOTH,
            "event": {
                "type": "string",
                "enum": ["any", "grabbed", "imported", "failed", "deleted"],
            },
            **paging.properties(),
        },
        (),
        "read",
        (
            "media history",
            "what was grabbed",
            "recent imports",
            "failed downloads",
            "what happened to",
        ),
        paged=True,
    ),
    _spec(
        "media_health",
        MEDIA_HEALTH,
        {},
        (),
        "read",
        (
            "media health",
            "is radarr ok",
            "is sonarr ok",
            "indexer status",
            "prowlarr health",
            "media stack",
        ),
    ),
    _spec(
        "movie_collections",
        MOVIE_COLLECTIONS,
        {"name": {"type": "string"}},
        (),
        "read",
        (
            "collections",
            "do i have all the",
            "the whole trilogy",
            "franchise",
            "which are missing from the set",
        ),
    ),
    _spec(
        "search_indexers",
        SEARCH_INDEXERS,
        {
            "query": {"type": "string"},
            "category": {"type": "string", "enum": ["any", "movies", "tv"]},
            **paging.properties(),
        },
        ("query",),
        "read",
        (
            "search indexers",
            "search torrents",
            "is there a release for",
            "what is out there",
            "prowlarr search",
        ),
        needs=("prowlarr",),
        paged=True,
    ),
]

PROWLARR_CATEGORIES = {"movies": [2000], "tv": [5000]}
# History event ids, per app, so the filter runs server-side and the count
# is the app's total. Radarr: grabbed 1, downloadFolderImported 3,
# downloadFailed 4, movieFileDeleted 6, movieFolderImported 7. Sonarr:
# grabbed 1, seriesFolderImported 2, downloadFolderImported 3, downloadFailed
# 4, episodeFileDeleted 5.
HISTORY_EVENTS = {
    "movie": {"grabbed": [1], "imported": [3, 7], "failed": [4], "deleted": [6]},
    "series": {"grabbed": [1], "imported": [2, 3], "failed": [4], "deleted": [5]},
}
# What the apps sort their wanted lists by; anything else is silently the default.
WANTED_SORT = {
    "movie": ("movieMetadata.sortTitle", "ascending"),
    "series": ("episodes.airDateUtc", "descending"),
}


def _num(value, missing=-1):
    """An integer field, with 0 kept as 0: `or -1` would lose season zero,
    the specials."""
    return missing if value is None else int(value)


def _gb(value):
    return round(int(value or 0) / GB, 2)


def _kinds(args):
    kind = str(args.get("kind") or "both")
    if kind == "both":
        return ["movie", "series"], None
    if kind not in KINDS:
        return None, {"ok": False, "error": "kind must be movie, series or both"}
    return [kind], None


def _movie_row(row):
    return {
        "title": row.get("title"),
        "year": row.get("year"),
        "tmdb_id": row.get("tmdbId"),
        "has_file": bool(row.get("hasFile")),
        "size_gb": _gb(row.get("sizeOnDisk")),
        "monitored": bool(row.get("monitored")),
        "added": str(row.get("added") or "")[:10],
        "genres": (row.get("genres") or [])[:3],
    }


def _series_row(row):
    stats = row.get("statistics") or {}
    return {
        "title": row.get("title"),
        "year": row.get("year"),
        "tvdb_id": row.get("tvdbId"),
        "episodes_held": stats.get("episodeFileCount"),
        "episodes_total": stats.get("totalEpisodeCount"),
        "size_gb": _gb(stats.get("sizeOnDisk")),
        "monitored": bool(row.get("monitored")),
        "status": row.get("status"),
        "added": str(row.get("added") or "")[:10],
        "genres": (row.get("genres") or [])[:3],
    }


def impls(ctx: ToolContext):
    media, operations = ctx.media, ctx.operations

    def _client(kind):
        return media._client(kind)

    def _row(kind, catalog_id):
        try:
            catalog_id = int(catalog_id)
        except (TypeError, ValueError):
            return None, {"ok": False, "error": "catalog_id must be an integer"}
        if catalog_id <= 0:
            return None, {"ok": False, "error": "catalog_id must be positive"}
        row = media._library_row(kind, catalog_id)
        if row is None:
            return None, {
                "ok": False,
                "error": f"that {kind} is not in the library - request it first",
            }
        return row, None

    def _kind(args):
        kind = str(args.get("kind") or "")
        if kind not in KINDS:
            return None, {"ok": False, "error": "kind must be movie or series"}
        return kind, None

    bind = Bindings(ctx, SPECS)

    def _join(submission, **extra):
        """Track work the service just accepted, joining the title's live
        operation when there is one, on the service's merge rule."""
        joined = operations_mod.join(
            operations, submission, media.merge_work, ctx.turn()
        )
        return {**joined, **extra}

    # -- reads ---------------------------------------------------------------

    @bind
    def browse_media(args):
        kind, err = _kind(args)
        if err:
            return err
        rows = [
            r
            for r in _client(kind).get(KINDS[kind]["resource"]) or []
            if isinstance(r, dict)
        ]
        genre = str(args.get("genre") or "").lower()
        if genre:
            rows = [
                r
                for r in rows
                if any(genre in str(g).lower() for g in (r.get("genres") or []))
            ]
        shape = _movie_row if kind == "movie" else _series_row
        items = [shape(r) for r in rows]
        if args.get("unmonitored_only"):
            items = [i for i in items if not i["monitored"]]
        sort = str(args.get("sort") or "recent")
        key = {
            "recent": lambda i: i["added"],
            "largest": lambda i: i["size_gb"],
            "title": lambda i: str(i["title"] or "").lower(),
            "year": lambda i: i["year"] or 0,
        }.get(sort)
        if key is None:
            return {"ok": False, "error": "sort must be recent, largest, title or year"}
        items.sort(key=key, reverse=sort in ("recent", "largest", "year"))
        return paging.page(items, args, "items", kind=kind)

    @bind
    def media_details(args):
        kind, err = _kind(args)
        if err:
            return err
        row, err = _row(kind, args.get("catalog_id"))
        if err:
            return err
        client = _client(kind)
        profiles = {
            int(p["id"]): p.get("name")
            for p in (client.get("qualityprofile") or [])
            if isinstance(p, dict) and "id" in p
        }
        out: dict[str, Any] = {
            "ok": True,
            "kind": kind,
            **(_movie_row(row) if kind == "movie" else _series_row(row)),
            "path": row.get("path"),
            "quality_profile": profiles.get(int(row.get("qualityProfileId", 0) or 0)),
        }
        if kind == "movie":
            files = client.get("moviefile", {"movieId": row["id"]}) or []
            out["files"] = [
                {
                    "path": f.get("relativePath"),
                    "size_gb": _gb(f.get("size")),
                    "quality": ((f.get("quality") or {}).get("quality") or {}).get(
                        "name"
                    ),
                }
                for f in files
                if isinstance(f, dict)
            ]
            return out
        episodes = client.get("episode", {"seriesId": row["id"]}) or []
        now = datetime.datetime.now(datetime.UTC)
        seasons: dict[int, dict] = {}
        for e in episodes:
            if not isinstance(e, dict):
                continue
            s = seasons.setdefault(
                int(e.get("seasonNumber", 0) or 0),
                {
                    "season": e.get("seasonNumber"),
                    "held": 0,
                    "missing": 0,
                    "upcoming": 0,
                    "monitored": False,
                },
            )
            air = _parse_time(str(e.get("airDateUtc") or "").replace("Z", "+00:00"))
            aired = air is not None and air <= now
            if e.get("hasFile"):
                s["held"] += 1
            elif aired:
                s["missing"] += 1
            else:
                s["upcoming"] += 1
            s["monitored"] = s["monitored"] or bool(e.get("monitored"))
        out["seasons"] = [seasons[k] for k in sorted(seasons)]
        return out

    @bind
    def missing_media(args):
        kind, err = _kind(args)
        if err:
            return err
        client = _client(kind)
        bounds, err = paging.window(args)
        if err:
            return err
        limit, offset = bounds
        sort_key, direction = WANTED_SORT[kind]
        # One window from the app for each list: the offset walks both.
        params = {
            "page": 1,
            "pageSize": offset + limit,
            "sortKey": sort_key,
            "sortDirection": direction,
        }
        if kind == "series":
            params["includeSeries"] = "true"
        missing = client.get("wanted/missing", params) or {}
        cutoff = client.get("wanted/cutoff", params) or {}

        def shape(r):
            if kind == "movie":
                return {
                    "title": r.get("title"),
                    "year": r.get("year"),
                    "tmdb_id": r.get("tmdbId"),
                }
            series = r.get("series") or {}
            return {
                "series": series.get("title"),
                "season": r.get("seasonNumber"),
                "episode": r.get("episodeNumber"),
                "title": r.get("title"),
                "aired": str(r.get("airDateUtc") or "")[:10],
            }

        missing_total = int(
            missing.get("totalRecords", len(missing.get("records", []))) or 0
        )
        cutoff_total = int(
            cutoff.get("totalRecords", len(cutoff.get("records", []))) or 0
        )
        after = offset + limit
        return {
            "ok": True,
            "kind": kind,
            "offset": offset,
            "missing_count": missing_total,
            "missing": [shape(r) for r in missing.get("records", [])[offset:after]],
            "below_cutoff_count": cutoff_total,
            "below_cutoff": [shape(r) for r in cutoff.get("records", [])[offset:after]],
            "next_offset": after if after < max(missing_total, cutoff_total) else None,
        }

    @bind
    def calendar(args):
        kinds, err = _kinds(args)
        if err:
            return err
        try:
            days = int(args.get("days") or 7)
        except (TypeError, ValueError):
            return {"ok": False, "error": "days must be an integer"}
        today = datetime.datetime.now(datetime.UTC).date()
        start, end = sorted((today, today + datetime.timedelta(days=days)))
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "includeSeries": "true",
            "includeMovie": "true",
        }
        rows: list[dict[str, Any]] = []
        for kind in kinds:
            for r in _client(kind).get("calendar", params) or []:
                if not isinstance(r, dict):
                    continue
                if kind == "movie":
                    # The app lists a film when ANY of its dates is in the
                    # window; show the one that is, not the first one set.
                    dates = [
                        str(r.get(k) or "")[:10]
                        for k in ("digitalRelease", "physicalRelease", "inCinemas")
                    ]
                    when = next(
                        (
                            d
                            for d in dates
                            if d and start.isoformat() <= d <= end.isoformat()
                        ),
                        next((d for d in dates if d), ""),
                    )
                    rows.append(
                        {
                            "kind": "movie",
                            "when": str(when or "")[:10],
                            "title": r.get("title"),
                            "year": r.get("year"),
                            "has_file": bool(r.get("hasFile")),
                        }
                    )
                else:
                    series = r.get("series") or {}
                    rows.append(
                        {
                            "kind": "episode",
                            "when": str(r.get("airDateUtc") or "")[:16],
                            "series": series.get("title"),
                            "season": r.get("seasonNumber"),
                            "episode": r.get("episodeNumber"),
                            "title": r.get("title"),
                            "has_file": bool(r.get("hasFile")),
                        }
                    )
        rows.sort(key=lambda r: str(r["when"]))
        return paging.page(
            rows, args, "items", **{"from": start.isoformat(), "to": end.isoformat()}
        )

    @bind
    def search_releases(args):
        kind, err = _kind(args)
        if err:
            return err
        row, err = _row(kind, args.get("catalog_id"))
        if err:
            return err
        client = _client(kind)
        if kind == "movie":
            params: dict[str, Any] = {"movieId": row["id"]}
        else:
            season = args.get("season")
            if season is None:
                return {"ok": False, "error": "a series search needs a season number"}
            params = {"seriesId": row["id"], "seasonNumber": int(season)}
            if args.get("episode") is not None:
                episodes = client.get("episode", {"seriesId": row["id"]}) or []
                match = [
                    e
                    for e in episodes
                    if _num(e.get("seasonNumber")) == int(season)
                    and _num(e.get("episodeNumber")) == int(args["episode"])
                ]
                if not match:
                    return {"ok": False, "error": "no such episode in that season"}
                params = {"episodeId": match[0]["id"]}
        releases = client.get("release", params) or []
        rows = []
        for r in releases:
            if not isinstance(r, dict):
                continue
            rows.append(
                {
                    "guid": r.get("guid"),
                    "indexer_id": r.get("indexerId"),
                    "indexer": r.get("indexer"),
                    "name": r.get("title"),
                    "size_gb": _gb(r.get("size")),
                    "seeders": r.get("seeders"),
                    "quality": ((r.get("quality") or {}).get("quality") or {}).get(
                        "name"
                    ),
                    "age_hours": round(float(r.get("ageHours", 0) or 0)),
                    "approved": bool(r.get("approved")),
                    "rejections": (r.get("rejections") or [])[:3],
                }
            )
        rows.sort(key=lambda r: (not r["approved"], -(r["seeders"] or 0)))
        return {
            "ok": True,
            "kind": kind,
            "title": row.get("title"),
            "count": len(rows),
            "releases": rows[:15],
        }

    @bind
    def import_queue(args):
        kinds, err = _kinds(args)
        if err:
            return err
        rows = []
        for kind in kinds:
            client = _client(kind)
            include = "includeMovie" if kind == "movie" else "includeSeries"
            queue = (
                client.get("queue", {"page": 1, "pageSize": 1000, include: "true"})
                or {}
            )
            for r in queue.get("records", []) if isinstance(queue, dict) else []:
                parent = r.get("movie" if kind == "movie" else "series") or {}
                size = float(r.get("size", 0) or 0)
                left = float(r.get("sizeleft", 0) or 0)
                rows.append(
                    {
                        "kind": kind,
                        "queue_id": r.get("id"),
                        "title": parent.get("title"),
                        "season": r.get("seasonNumber") if kind == "series" else None,
                        "status": r.get("status"),
                        "tracked": r.get("trackedDownloadState"),
                        "problem": r.get("trackedDownloadStatus")
                        if r.get("trackedDownloadStatus") not in (None, "ok")
                        else None,
                        "percent": round(100 * (size - left) / size) if size else None,
                        "warnings": [
                            f"{m.get('title')}: {text}" if m.get("title") else str(text)
                            for m in (r.get("statusMessages") or [])
                            for text in (m.get("messages") or [m.get("title")])
                        ][:3],
                        "download_id": r.get("downloadId"),
                    }
                )
        return paging.page(rows, args, "items")

    @bind
    def media_history(args):
        kinds, err = _kinds(args)
        if err:
            return err
        event = str(args.get("event") or "any")
        if event != "any" and event not in HISTORY_EVENTS["movie"]:
            return {
                "ok": False,
                "error": "event must be any, grabbed, imported, failed or deleted",
            }
        bounds, err = paging.window(args)
        if err:
            return err
        limit, offset = bounds
        rows = []
        total = 0
        for kind in kinds:
            client = _client(kind)
            include = "includeMovie" if kind == "movie" else "includeSeries"
            # Each app serves its own newest window; the merge below is
            # sliced to the page, so the offset is honoured across both.
            params: dict[str, Any] = {
                "page": 1,
                "pageSize": offset + limit,
                "sortKey": "date",
                "sortDirection": "descending",
                include: "true",
            }
            if event != "any":
                # Server-side, so the count is the app's total, not a window.
                params["eventType"] = HISTORY_EVENTS[kind][event]
            history = client.get("history", params) or {}
            if not isinstance(history, dict):
                continue
            records = history.get("records", [])
            total += int(history.get("totalRecords", len(records)) or 0)
            for r in records:
                parent = r.get("movie" if kind == "movie" else "series") or {}
                rows.append(
                    {
                        "kind": kind,
                        "when": str(r.get("date") or "")[:16],
                        "event": r.get("eventType"),
                        "title": parent.get("title"),
                        "release": r.get("sourceTitle"),
                        "quality": ((r.get("quality") or {}).get("quality") or {}).get(
                            "name"
                        ),
                    }
                )
        rows.sort(key=lambda r: r["when"], reverse=True)
        return paging.page(rows[offset:], args, "items", total=total)

    @bind
    def media_health(args):
        out: dict[str, Any] = {"ok": True, "health": [], "indexers": []}
        clients = [media.radarr, media.sonarr] + (
            [media.prowlarr] if media.prowlarr else []
        )
        for client in clients:
            try:
                for h in client.get("health") or []:
                    out["health"].append(
                        {
                            "app": client.name,
                            "type": h.get("type"),
                            "source": h.get("source"),
                            "message": h.get("message"),
                        }
                    )
            except MediaError as e:
                out["health"].append(
                    {
                        "app": client.name,
                        "type": "error",
                        "message": f"unreachable: {e}",
                    }
                )
        if media.prowlarr:
            try:
                names = {
                    int(i["id"]): i.get("name")
                    for i in (media.prowlarr.get("indexer") or [])
                    if isinstance(i, dict) and "id" in i
                }
                for s in media.prowlarr.get("indexerstatus") or []:
                    out["indexers"].append(
                        {
                            "indexer": names.get(int(s.get("indexerId", 0) or 0)),
                            "disabled_till": s.get("disabledTill"),
                            "failure": s.get("mostRecentFailure"),
                        }
                    )
            except MediaError as e:
                out["indexers"].append(
                    {"indexer": None, "failure": f"prowlarr unreachable: {e}"}
                )
        out["healthy"] = not out["health"] and not out["indexers"]
        return out

    @bind
    def movie_collections(args):
        want = str(args.get("name") or "").lower()
        collections = media.radarr.get("collection") or []
        held = {
            int(m.get("tmdbId", 0) or 0)
            for m in (media.radarr.get("movie") or [])
            if isinstance(m, dict) and m.get("hasFile")
        }
        rows = []
        for c in collections:
            if not isinstance(c, dict):
                continue
            if want and want not in str(c.get("title", "")).lower():
                continue
            movies = c.get("movies") or []
            missing = [
                m.get("title")
                for m in movies
                if int(m.get("tmdbId", 0) or 0) not in held
            ]
            rows.append(
                {
                    "collection": c.get("title"),
                    "movies": len(movies),
                    "held": len(movies) - len(missing),
                    "missing": missing[:10],
                    "monitored": bool(c.get("monitored")),
                }
            )
        rows.sort(key=lambda r: str(r["collection"] or "").lower())
        return {"ok": True, "count": len(rows), "collections": rows[: paging.CAP]}

    @bind
    def search_indexers(args):
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "search needs a query"}
        category = str(args.get("category") or "any")
        if category not in ("any", "movies", "tv"):
            return {"ok": False, "error": "category must be any, movies or tv"}
        params: dict[str, Any] = {"query": query[:120], "type": "search", "limit": 50}
        if category != "any":
            params["categories"] = PROWLARR_CATEGORIES[category]
        results = media.prowlarr.get("search", params) or []
        rows = [
            {
                "name": r.get("title"),
                "indexer": r.get("indexer"),
                "size_gb": _gb(r.get("size")),
                "seeders": r.get("seeders"),
                "age_days": r.get("age"),
                "guid": r.get("guid"),
                "indexer_id": r.get("indexerId"),
            }
            for r in results
            if isinstance(r, dict)
        ]
        rows.sort(key=lambda r: -(r["seeders"] or 0))
        return paging.page(rows, args, "releases", query=query)

    # -- acts ----------------------------------------------------------------

    @bind
    def grab_release(args):
        kind, err = _kind(args)
        if err:
            return err
        guid = str(args.get("guid") or "").strip()
        try:
            catalog_id = int(args.get("catalog_id"))
            indexer_id = int(args.get("indexer_id"))
            season = None if args.get("season") is None else int(args["season"])
            episode = None if args.get("episode") is None else int(args["episode"])
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "catalog_id, indexer_id, season and episode must be integers",
            }
        if not guid:
            return {"ok": False, "error": "pass the release guid from search_releases"}
        if dry := ctx.preview(f"grab {kind} release {guid[:40]}"):
            return dry
        return _join(
            media.grab_release(kind, catalog_id, guid, indexer_id, season, episode)
        )

    @bind
    def retry_search(args):
        kind, err = _kind(args)
        if err:
            return err
        try:
            catalog_id = int(args.get("catalog_id"))
            season = None if args.get("season") is None else int(args["season"])
            episode = None if args.get("episode") is None else int(args["episode"])
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "catalog_id, season and episode must be integers",
            }
        if kind == "movie":
            season = episode = None
        scope = (
            "all of it"
            if season is None
            else f"season {season}" + (f" episode {episode}" if episode else "")
        )
        if dry := ctx.preview(f"search again for {kind} {catalog_id}, {scope}"):
            return dry
        return _join(media.search_again(kind, catalog_id, season, episode))

    @bind
    def set_monitored(args):
        kind, err = _kind(args)
        if err:
            return err
        row, err = _row(kind, args.get("catalog_id"))
        if err:
            return err
        monitored = args.get("monitored")
        if not isinstance(monitored, bool):
            return {"ok": False, "error": "monitored must be true or false"}
        seasons = args.get("seasons")
        if seasons is not None and (
            kind == "movie" or not isinstance(seasons, list) or not seasons
        ):
            return {
                "ok": False,
                "error": "seasons applies to a series and must be a non-empty list",
            }
        scope = f"seasons {seasons}" if seasons else "everything"
        if dry := ctx.preview(
            f"set monitored={monitored} on {row.get('title')} {scope}"
        ):
            return dry
        # A deep copy: the row's season dicts came from the app's own answer.
        updated = json.loads(json.dumps(row))
        if kind == "movie" or not seasons:
            # The whole title: only its own flag, as the app's UI does. Touching
            # every season would rewrite every episode's flag underneath.
            updated["monitored"] = monitored
        else:
            wanted = {int(s) for s in seasons}
            for s in updated.get("seasons") or []:
                if _num(s.get("seasonNumber")) in wanted:
                    s["monitored"] = monitored
            if monitored:
                updated["monitored"] = True
        _client(kind).put(f"{KINDS[kind]['resource']}/{row['id']}", updated)
        return {
            "ok": True,
            "title": row.get("title"),
            "monitored": monitored,
            "scope": scope,
        }

    @bind
    def set_quality_profile(args):
        kind, err = _kind(args)
        if err:
            return err
        row, err = _row(kind, args.get("catalog_id"))
        if err:
            return err
        preset = str(args.get("preset") or "")
        profile_id, profile_name = media._profile(kind, preset)
        if dry := ctx.preview(f"set profile {profile_name} on {row.get('title')}"):
            return dry
        if int(row.get("qualityProfileId", 0) or 0) == profile_id:
            return {
                "ok": True,
                "title": row.get("title"),
                "profile": profile_name,
                "changed": False,
            }
        updated = dict(row, qualityProfileId=profile_id)
        _client(kind).put(f"{KINDS[kind]['resource']}/{row['id']}", updated)
        return {
            "ok": True,
            "title": row.get("title"),
            "profile": profile_name,
            "changed": True,
        }

    @bind.destructive
    def resolve_queue_item(args):
        kind, err = _kind(args)
        if err:
            return err
        try:
            queue_id = int(args.get("queue_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "queue_id must be an integer"}
        remove = bool(args.get("remove_from_client", True))
        blocklist = bool(args.get("blocklist", False))
        # Marking a release failed makes the app remove the download itself
        # (its default) and grab another, so blocklist erases data too.
        destructive = remove or blocklist
        client = _client(kind)
        include = "includeMovie" if kind == "movie" else "includeSeries"
        queue = (
            client.get("queue", {"page": 1, "pageSize": 1000, include: "true"}) or {}
        )
        item = next(
            (
                r
                for r in queue.get("records", [])
                if int(r.get("id", 0) or 0) == queue_id
            ),
            None,
        )
        if item is None:
            return {
                "ok": False,
                "error": "no queue item with that id - it may have finished",
            }
        parent = item.get("movie" if kind == "movie" else "series") or {}
        title = parent.get("title") or f"queue item {queue_id}"
        params = {
            "removeFromClient": "true" if remove else "false",
            "blocklist": "true" if blocklist else "false",
        }

        def act():
            client.delete(f"queue/{queue_id}", params)
            out = {
                "ok": True,
                "title": title,
                "removed_from_client": remove,
                "blocklisted": blocklist,
            }
            if not destructive:
                out["detail"] = (
                    "the queue item is forgotten; the torrent stays in qBittorrent "
                    "unowned - delete_torrent removes it"
                )
            return out

        preview = f"remove queue {kind}/{queue_id} for {title} {params}"
        if not destructive:
            # Forgetting alone erases nothing: no question to ask.
            return ctx.preview(preview) or act()
        if blocklist:
            ask = (
                f"Mark the release for {title} as failed, which erases what has "
                "arrived, never takes that release again, and looks for another?"
            )
        else:
            ask = f"Cancel the download for {title} and erase what has arrived?"
        return Plan(("queue", kind, queue_id, remove, blocklist), ask, act, preview)

    @bind
    def manual_import(args):
        kind, err = _kind(args)
        if err:
            return err
        download_id = str(args.get("download_id") or "").strip()
        if not download_id:
            return {"ok": False, "error": "pass the download id from import_queue"}
        found = media.import_candidates(kind, download_id)
        files = found["files"]
        if found["unmatched"] or found["rejected"] or not files:
            return {
                "ok": False,
                "error": "the app could not match or would reject some files, "
                "so nothing was imported",
                "unmatched": found["unmatched"][:10],
                "rejected": found["rejected"][:10],
                "matched": len(files),
            }
        if dry := ctx.preview(f"manual import {len(files)} file(s) for {download_id}"):
            return dry
        return _join(media.manual_import(kind, download_id, found), files=len(files))

    return bind.impls()
