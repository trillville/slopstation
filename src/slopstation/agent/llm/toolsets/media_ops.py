"""Tools that change movies and TV through Radarr and Sonarr: take a release
by hand, retry a search, cancel a request, change what is monitored or at what
quality, clear a stuck queue item, and import by hand. Everything goes through
the arr apps, which own the downloads. The read-only tools are media_browse.
"""

from __future__ import annotations

import json

from slopstation.agent.llm.registry import Bindings, Plan, ToolContext, ToolSpec
from slopstation.agent.tools import operations as operations_mod
from slopstation.agent.tools.media_clients import KINDS

KIND = {"type": "string", "enum": ["movie", "series"]}
CATALOG_ID = {
    "type": "integer",
    "description": "TMDB movie id or TVDB series id from find_media",
}


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

CANCEL_REQUEST = """\
Stop a running media request by its operation id from list_operations: the
app stops looking for what it has not found, the searches that have not
started are cancelled, and its downloads in flight are removed. What it
already imported is KEPT - this is the tool for "stop that, I asked for the
wrong thing", and delete_media is the one that erases files. When downloads
would be thrown away the first call answers with the question to put to the
user, and acts only when called again unchanged after they say yes."""

SET_MONITORED = """\
Monitor or unmonitor a movie, a whole series, or named seasons. Unmonitored
items are kept but never searched for or upgraded; monitored items are."""

SET_QUALITY_PROFILE = """\
Change one title's quality profile to a configured preset: default, 1080p or
2160p. Only presets; the app then upgrades or holds according to it."""


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


def _spec(
    name,
    description,
    props,
    required,
    risk,
    keywords,
    needs=("media",),
    paged=False,
    busy=None,
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
        busy=busy,
    )


SPECS = [
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
        busy="grabbing it",
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
        busy="starting the search",
    ),
    _spec(
        "cancel_request",
        CANCEL_REQUEST,
        {
            "operation_id": {
                "type": "string",
                "description": "the id of an active request, from list_operations",
            }
        },
        ("operation_id",),
        "destructive",
        (
            "cancel the request",
            "stop the download",
            "stop searching for",
            "call off",
            "i did not mean to ask for that",
            "never mind that download",
        ),
        needs=("media", "operations"),
        busy="stopping it",
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
        busy="importing",
    ),
]


def _num(value, missing=-1):
    """An integer field, with 0 kept as 0: `or -1` would lose season zero,
    the specials."""
    return missing if value is None else int(value)


def _library_row(media, kind, catalog_id):
    """(row, None) for a title in the library, or (None, error dict)."""
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
    """(kind, None) for movie or series, or (None, error dict)."""
    kind = str(args.get("kind") or "")
    if kind not in KINDS:
        return None, {"ok": False, "error": "kind must be movie or series"}
    return kind, None


def impls(ctx: ToolContext):
    media, operations = ctx.media, ctx.operations

    def _client(kind):
        return media._client(kind)

    def _row(kind, catalog_id):
        return _library_row(media, kind, catalog_id)

    bind = Bindings(ctx, SPECS)

    def _track(submission, **extra):
        return {**operations_mod.track(operations, submission, ctx.turn()), **extra}

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
        return _track(
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
        return _track(media.search_again(kind, catalog_id, season, episode))

    @bind.destructive
    def cancel_request(args):
        operation_id = str(args.get("operation_id") or "").strip()
        if not operation_id:
            return {"ok": False, "error": "operation_id is required"}
        operation = operations.get(operation_id)
        if operation is None:
            return {
                "ok": False,
                "error": f"no operation with id {operation_id} - "
                "list_operations names the current ones",
            }
        if operation.get("kind") not in ("movie_acquisition", "series_acquisition"):
            return {
                "ok": False,
                "error": "only a movie or series request is cancelled here",
            }
        if operation.get("state") in operations_mod.TERMINAL:
            return {
                "ok": False,
                "error": f"that request already finished: "
                f"{str(operation['state']).lower()}",
            }
        title = operation.get("title") or "that request"
        targets = media.cancel_targets(operation)

        def act():
            result = media.cancel_request(operation)
            kept = (
                f"; {result['have']} already imported and kept"
                if result["have"]
                else ""
            )
            operations_mod.record_canceled(
                operations, operation, f"the request was canceled{kept}"
            )
            return {
                **result,
                "title": title,
                "operation_id": operation_id,
                "acknowledgment": f"Stopped the {title} request{kept}.",
            }

        preview = f"cancel operation {operation_id} for {title}"
        if not targets["downloads"]:
            # Nothing in flight to lose, so nothing to ask.
            return ctx.preview(preview) or act()
        downloads = targets["downloads"]
        noun = "download" if downloads == 1 else "downloads"
        return Plan(
            ("cancel", operation_id),
            f"Stop the {title} request? That erases {downloads} {noun} in "
            f"progress. Anything already imported is kept.",
            act,
            preview,
        )

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
        return _track(media.manual_import(kind, download_id, found), files=len(files))

    return bind.impls()
