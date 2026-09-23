"""Read-only tools over Radarr, Sonarr and Prowlarr: what is held, what is
missing or coming, releases, the queue, history, health and collections."""

from __future__ import annotations

import datetime
from typing import Any

from slopstation.agent.llm import paging
from slopstation.agent.llm.registry import Bindings, ToolContext
from slopstation.agent.llm.toolsets.media_schema import (
    CATALOG_ID,
    KIND,
    media_spec,
)
from slopstation.agent.media.clients import (
    KINDS,
    SEARCH_TIMEOUT_S,
    MediaError,
    parse_time,
)
from slopstation.agent.media.units import gigabytes

KIND_OR_BOTH = {"type": "string", "enum": ["movie", "series", "both"]}


BROWSE_MEDIA = """\
Browse the library: movies or series, sorted by recent additions, largest on
disk, title or year, optionally filtered to one genre or to unmonitored
items only. Returns the count and up to `limit` rows with title, year,
catalog id, whether files are held, size and monitored state. For ONE named
title use media_library instead."""


MEDIA_DETAILS = """\
One movie or series in full, by the catalog id from find_media: files with
their quality and size, path, monitored state, the quality profile with the
qualities it actually allows (most preferred first) and the upgrade-until
cutoff, and for a series each season's held, missing and upcoming episode
counts. This is the read for 'why was that release rejected' - a raw
profile read lists every quality, wanted or not."""


EPISODE_FILES = """\
Which episodes of one series hold a file, by the catalog id from find_media:
season and episode number, title, quality, size, and the date the file
arrived. Optionally one season. The only tool that names the episodes and
their dates; media_library and media_details count per season instead."""


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


IMPORT_QUEUE = """\
Downloads Radarr and Sonarr are tracking: state, progress, and any warning
about why one is stuck (waiting to import, unknown files, import failed).
Returns the count and up to `limit` rows; each carries its queue id for
resolve_queue_item and its download id for manual_import."""


MEDIA_HISTORY = """\
Recent events in Radarr and Sonarr: grabs, imports, failures, deletions, with
the title, release, quality and time. Filter by event and cap with `limit`;
returns the count and the rows, newest first."""


MEDIA_HEALTH = """\
Health messages from Radarr, Sonarr and Prowlarr, and each Prowlarr indexer's
status (disabled until when, last failure). healthy is false only for a live
problem: a health message, or an indexer disabled right now; an old failure on
an indexer that has recovered is listed but does not count. The answer to 'is
the media stack OK'. Read only."""


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


PROWLARR_CATEGORIES = {"movies": [2000], "tv": [5000]}


# History event ids, filtered server-side so the count is the app's total.
# Radarr: grabbed 1, imported 3 and 7, failed 4, deleted 6.
# Sonarr: grabbed 1, imported 2 and 3, failed 4, deleted 5.
HISTORY_EVENTS = {
    "movie": {"grabbed": [1], "imported": [3, 7], "failed": [4], "deleted": [6]},
    "series": {"grabbed": [1], "imported": [2, 3], "failed": [4], "deleted": [5]},
}


# What the apps sort their wanted lists by; anything else is silently the default.
WANTED_SORT = {
    "movie": ("movieMetadata.sortTitle", "ascending"),
    "series": ("episodes.airDateUtc", "descending"),
}


def _quality_name(row):
    return ((row.get("quality") or {}).get("quality") or {}).get("name")


def _profile_qualities(profile):
    """The qualities a profile allows, most preferred first, and the name of
    its upgrade-until cutoff. The app lists items least preferred first, a
    group carrying its members under one allowed flag."""
    allowed: list[Any] = []
    cutoff = None
    for item in profile.get("items") or []:
        if not isinstance(item, dict):
            continue
        members = item.get("items") or [item]
        own = item.get("quality") or {}
        ident = item.get("id") if item.get("items") else own.get("id")
        if ident is not None and ident == profile.get("cutoff"):
            cutoff = item.get("name") or own.get("name")
        if item.get("allowed"):
            allowed.extend(
                (m.get("quality") or {}).get("name")
                for m in members
                if isinstance(m, dict)
            )
    allowed.reverse()
    return allowed, cutoff


def _kinds(args):
    kind = str(args.get("kind") or "both")
    return ["movie", "series"] if kind == "both" else [kind]


def _movie_row(row):
    return {
        "title": row.get("title"),
        "year": row.get("year"),
        "tmdb_id": row.get("tmdbId"),
        "has_file": bool(row.get("hasFile")),
        "size_gb": gigabytes(row.get("sizeOnDisk")),
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
        "size_gb": gigabytes(stats.get("sizeOnDisk")),
        "monitored": bool(row.get("monitored")),
        "status": row.get("status"),
        "added": str(row.get("added") or "")[:10],
        "genres": (row.get("genres") or [])[:3],
    }


SPECS = [
    media_spec(
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
        risk="read",
        keywords=(
            "browse movies",
            "browse shows",
            "recently added",
            "largest movies",
            "what movies do i have",
            "by genre",
        ),
        paged=True,
        busy="checking the library",
    ),
    media_spec(
        "media_details",
        MEDIA_DETAILS,
        {"kind": KIND, "catalog_id": CATALOG_ID},
        ("kind", "catalog_id"),
        risk="read",
        keywords=(
            "movie details",
            "series details",
            "what quality is it",
            "which episodes are missing",
            "file size",
        ),
        busy="checking the library",
    ),
    media_spec(
        "episode_files",
        EPISODE_FILES,
        {
            "catalog_id": CATALOG_ID,
            "season": {
                "type": "integer",
                "description": "one season number; omit for the whole series",
            },
            **paging.properties(what="episodes"),
        },
        ("catalog_id",),
        risk="read",
        keywords=(
            "which episodes do i have",
            "episode files",
            "when was it downloaded",
            "what did that request bring in",
        ),
        paged=True,
    ),
    media_spec(
        "missing_media",
        MISSING_MEDIA,
        {"kind": KIND, **paging.properties()},
        ("kind",),
        risk="read",
        keywords=(
            "missing episodes",
            "missing movies",
            "wanted",
            "below cutoff",
            "not downloaded yet",
        ),
        paged=True,
        busy="checking what's missing",
    ),
    media_spec(
        "calendar",
        CALENDAR,
        {"kind": KIND_OR_BOTH, "days": {"type": "integer"}, **paging.properties()},
        (),
        risk="read",
        keywords=(
            "what airs this week",
            "calendar",
            "upcoming episodes",
            "coming out",
            "when is the next episode",
        ),
        paged=True,
        busy="checking the calendar",
    ),
    media_spec(
        "search_releases",
        SEARCH_RELEASES,
        {
            "kind": KIND,
            "catalog_id": CATALOG_ID,
            "season": {"type": "integer"},
            "episode": {"type": "integer"},
        },
        ("kind", "catalog_id"),
        risk="read",
        keywords=(
            "search releases",
            "which releases are available",
            "manual search",
            "pick a release",
            "interactive search",
        ),
        busy="searching the indexers",
    ),
    media_spec(
        "import_queue",
        IMPORT_QUEUE,
        {"kind": KIND_OR_BOTH, **paging.properties()},
        (),
        risk="read",
        keywords=(
            "import queue",
            "stuck import",
            "waiting to import",
            "queue warnings",
            "why is it stuck",
        ),
        paged=True,
        busy="checking the queue",
    ),
    media_spec(
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
        risk="read",
        keywords=(
            "media history",
            "what was grabbed",
            "recent imports",
            "failed downloads",
            "what happened to",
        ),
        paged=True,
        busy="checking the history",
    ),
    media_spec(
        "media_health",
        MEDIA_HEALTH,
        {},
        (),
        risk="read",
        keywords=(
            "media health",
            "is radarr ok",
            "is sonarr ok",
            "indexer status",
            "prowlarr health",
            "media stack",
        ),
        busy="checking the media stack",
    ),
    media_spec(
        "movie_collections",
        MOVIE_COLLECTIONS,
        {"name": {"type": "string"}},
        (),
        risk="read",
        keywords=(
            "collections",
            "do i have all the",
            "the whole trilogy",
            "franchise",
            "which are missing from the set",
        ),
        busy="checking the collections",
    ),
    media_spec(
        "search_indexers",
        SEARCH_INDEXERS,
        {
            "query": {"type": "string"},
            "category": {"type": "string", "enum": ["any", "movies", "tv"]},
            **paging.properties(),
        },
        ("query",),
        risk="read",
        keywords=(
            "search indexers",
            "search torrents",
            "is there a release for",
            "what is out there",
            "prowlarr search",
        ),
        needs=("prowlarr",),
        paged=True,
        busy="searching the indexers",
    ),
]


def impls(ctx: ToolContext):
    media = ctx.media

    bind = Bindings(ctx, SPECS)

    @bind
    def browse_media(args):
        kind = str(args.get("kind"))
        rows = [
            r
            for r in media._client(kind).get(KINDS[kind]["resource"]) or []
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
        kind = str(args.get("kind"))
        row = media._held(kind, int(args["catalog_id"]))
        client = media._client(kind)
        profiles = {
            int(p["id"]): p
            for p in (client.get("qualityprofile") or [])
            if isinstance(p, dict) and "id" in p
        }
        profile = profiles.get(int(row.get("qualityProfileId", 0) or 0)) or {}
        allowed, cutoff = _profile_qualities(profile)
        out: dict[str, Any] = {
            "ok": True,
            "kind": kind,
            **(_movie_row(row) if kind == "movie" else _series_row(row)),
            "path": row.get("path"),
            "quality_profile": profile.get("name"),
            "allowed_qualities": allowed,
            "upgrade_until": cutoff,
        }
        if kind == "movie":
            files = client.get("moviefile", {"movieId": row["id"]}) or []
            out["files"] = [
                {
                    "path": f.get("relativePath"),
                    "size_gb": gigabytes(f.get("size")),
                    "quality": _quality_name(f),
                }
                for f in files
                if isinstance(f, dict)
            ]
            return out
        out["seasons"] = media.season_counts(row["id"])
        return out

    @bind
    def episode_files(args):
        row = media._held("series", int(args["catalog_id"]))
        try:
            season = None if args.get("season") is None else int(args["season"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "season must be an integer"}
        params = {"seriesId": row["id"], "includeEpisodeFile": "true"}
        if season is not None:
            params["seasonNumber"] = season
        episodes = media._client("series").get("episode", params) or []
        held = []
        for e in episodes:
            if not isinstance(e, dict) or not e.get("hasFile"):
                continue
            f = e.get("episodeFile") or {}
            held.append(
                {
                    "season": int(e.get("seasonNumber", 0) or 0),
                    "episode": int(e.get("episodeNumber", 0) or 0),
                    "title": e.get("title"),
                    "quality": _quality_name(f),
                    "size_gb": gigabytes(f.get("size")),
                    "added": str(f.get("dateAdded") or "")[:19],
                }
            )
        held.sort(key=lambda r: (r["season"], r["episode"]))
        return paging.page(
            held, args, "episodes", title=row.get("title"), season=season
        )

    @bind
    def missing_media(args):
        kind = str(args.get("kind"))
        client = media._client(kind)
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
        kinds = _kinds(args)
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
            for r in media._client(kind).get("calendar", params) or []:
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
        kind = str(args.get("kind"))
        row = media._held(kind, int(args["catalog_id"]))
        client = media._client(kind)
        if kind == "movie":
            params: dict[str, Any] = {"movieId": row["id"]}
        else:
            season = args.get("season")
            if season is None:
                return {"ok": False, "error": "a series search needs a season number"}
            params = {"seriesId": row["id"], "seasonNumber": int(season)}
            if args.get("episode") is not None:
                episode_id = media.episode_id(row["id"], season, args["episode"])
                if episode_id is None:
                    return {"ok": False, "error": "no such episode in that season"}
                params = {"episodeId": episode_id}
        releases = client.get("release", params, timeout=SEARCH_TIMEOUT_S) or []
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
                    "size_gb": gigabytes(r.get("size")),
                    "seeders": r.get("seeders"),
                    "quality": _quality_name(r),
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
        kinds = _kinds(args)
        rows = []
        for kind in kinds:
            client = media._client(kind)
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
        kinds = _kinds(args)
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
            client = media._client(kind)
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
                        "quality": _quality_name(r),
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
                # Every indexer that ever failed has a row; only a disabledTill
                # still ahead means it is down now.
                now = datetime.datetime.now(datetime.UTC)
                for s in media.prowlarr.get("indexerstatus") or []:
                    till = parse_time(s.get("disabledTill"))
                    out["indexers"].append(
                        {
                            "indexer": names.get(int(s.get("indexerId", 0) or 0)),
                            "disabled": bool(till and till > now),
                            "disabled_till": s.get("disabledTill"),
                            "failure": s.get("mostRecentFailure"),
                        }
                    )
            except MediaError as e:
                out["indexers"].append(
                    {
                        "indexer": None,
                        "disabled": True,
                        "failure": f"prowlarr unreachable: {e}",
                    }
                )
        out["healthy"] = not out["health"] and not any(
            i["disabled"] for i in out["indexers"]
        )
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
                "size_gb": gigabytes(r.get("size")),
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

    return bind.impls()
