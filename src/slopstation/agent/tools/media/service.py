"""MediaService: the one object callers hold, over movies and series."""

import json

from slopstation.agent.tools.media.movies import _Movies
from slopstation.agent.tools.media.series import _Series
from slopstation.agent.tools.media_clients import (
    MediaError,
    _clean_text,
    _kind,
)


class MediaService(_Series, _Movies):
    """Resolve policy names and submit/observe concrete media requests."""

    def find(self, kind, query):
        query = _clean_text(query)
        if not query:
            raise MediaError("media lookup needs a title")
        spec = _kind(kind)
        client = self._client(kind)
        rows = client.get(f"{spec['resource']}/lookup", {"term": query})
        if not isinstance(rows, list):
            raise MediaError(f"{client.name} returned an invalid lookup")
        out = []
        id_key = spec["id_key"]
        public_key = spec["public_key"]
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                catalog_id = int(row.get(id_key, 0))
                year = int(row.get("year", 0))
            except (TypeError, ValueError):
                continue
            title = _clean_text(row.get("title"))
            if catalog_id <= 0 or not title:
                continue
            out.append(
                {
                    public_key: catalog_id,
                    "title": title,
                    "year": year,
                    "status": _clean_text(row.get("status"), 40),
                }
            )
            if len(out) == 5:
                break
        return out

    def library(self, kind, catalog_id):
        try:
            catalog_id = int(catalog_id)
        except (TypeError, ValueError) as e:
            raise MediaError("catalog id must be an integer") from e
        if catalog_id <= 0:
            raise MediaError("catalog id must be positive")
        row = self._library_row(kind, catalog_id)
        if row is None:
            return {
                "kind": kind,
                "catalog_id": catalog_id,
                "in_library": False,
                # Names a wrong id before a request acts on it.
                "title": self._catalog_title(kind, catalog_id),
            }
        if kind == "movie":
            return {
                "kind": kind,
                "catalog_id": catalog_id,
                "in_library": True,
                "title": _clean_text(row.get("title")) or f"TMDB {catalog_id}",
                "year": row.get("year"),
                "available": bool(row.get("hasFile")),
            }
        series = row
        rows = self.sonarr.get("episode", {"seriesId": int(series["id"])})
        seasons: dict[int, dict] = {}
        for episode in self._target_episodes(rows, monitored_only=False):
            number = int(episode.get("seasonNumber", 0) or 0)
            row = seasons.setdefault(number, {"season": number, "have": 0, "aired": 0})
            row["aired"] += 1
            if episode.get("hasFile"):
                row["have"] += 1
        return {
            "kind": kind,
            "catalog_id": catalog_id,
            "in_library": True,
            "title": _clean_text(series.get("title")) or f"TVDB {catalog_id}",
            "year": series.get("year"),
            "seasons": [seasons[number] for number in sorted(seasons)],
        }

    def search_available(self, operation):
        client = self._client(self._operation_kind(operation))
        indexers = client.get("indexer")
        health = client.get("health")
        if not isinstance(indexers, list) or not isinstance(health, list):
            raise MediaError(f"{client.name} returned invalid indexer health")
        enabled = any(
            isinstance(row, dict)
            and row.get("enable", True)
            and row.get("enableAutomaticSearch", True)
            for row in indexers
        )
        blocked = any(
            isinstance(row, dict)
            and str(row.get("source", "")).casefold() == "indexersearchcheck"
            for row in health
        )
        return enabled and not blocked

    def abandon_missing(self, operation):
        """Unmonitor the still-missing scope so the authority stops watching."""
        missing, have = self._missing_scope(operation)
        if self._operation_kind(operation) == "movie":
            for movie in missing:
                unmonitored = dict(movie)
                unmonitored["monitored"] = False
                self.radarr.put(f"movie/{int(movie['id'])}", unmonitored)
            return {"have": have, "missing": [], "episode_ids": []}
        episode_ids = self._episode_ids(missing)
        self._monitor_episodes(episode_ids, False)
        by_season: dict = {}
        for row in missing:
            number = int(row.get("seasonNumber", 0) or 0)
            by_season[number] = by_season.get(number, 0) + 1
        return {
            "have": have,
            "missing": [
                {"season": number, "episodes": by_season[number]}
                for number in sorted(by_season)
            ],
            "episode_ids": episode_ids,
        }

    def _scope_queue(self, operation, episode_ids=None):
        """The queue rows for an operation's scope, filtered to the episodes
        it owns so a cancel leaves another request's download alone."""
        kind = self._operation_kind(operation)
        client = self._client(kind)
        row_id = int(operation["external_ref"])
        if kind == "movie":
            return self._queue_records(client, "movieId", row_id)
        if episode_ids is None:
            missing, _ = self._missing_scope(operation)
            episode_ids = self._episode_ids(missing)
        wanted = set(episode_ids)
        return [
            row
            for row in self._queue_records(client, "seriesId", row_id)
            if int(row.get("episodeId", 0) or 0) in wanted
        ]

    def cancel_targets(self, operation):
        """What a cancel would act on, for the question put to the user.
        `cancel_request` resolves it again when they answer."""
        missing, have = self._missing_scope(operation)
        episode_ids = (
            None
            if self._operation_kind(operation) == "movie"
            else self._episode_ids(missing)
        )
        queue = self._scope_queue(operation, episode_ids)
        return {
            "have": have,
            "missing": len(missing),
            "downloads": len({self._download_key(row) for row in queue}),
        }

    def cancel_request(self, operation):
        """Stop an acquisition, keeping what it already imported: unmonitor
        what is missing, recall the searches that have not started, drop that
        scope's downloads.

        That order matters. A started search cannot be recalled, but it
        rechecks `monitored` before it grabs, so unmonitoring first is what
        makes it harmless; dropping the queue rows first would let it grab
        them again."""
        kind = self._operation_kind(operation)
        client = self._client(kind)
        abandoned = self.abandon_missing(operation)
        searches = self._cancel_commands(
            client, (operation.get("metadata") or {}).get("command_ids")
        )
        queue = self._scope_queue(operation, abandoned["episode_ids"])
        downloads = self._remove_queue(client, queue)
        unmonitored = (
            len(abandoned["episode_ids"]) if kind == "series" else 1 - abandoned["have"]
        )
        return {
            "ok": True,
            "kind": kind,
            "have": abandoned["have"],
            "unmonitored": unmonitored,
            "downloads_canceled": downloads,
            "searches_canceled": searches["canceled"],
            "searches_running": searches["running"],
        }

    def retry_search(self, operation):
        metadata = operation.get("metadata") or {}
        return self._search(
            self._operation_kind(operation),
            operation["external_ref"],
            self._seasons(metadata.get("seasons")),
            metadata.get("episode_ids"),
        )

    # -- work on a title the library already holds ------------------------------

    def _held(self, kind, catalog_id):
        """The app's row for a catalog id, or the plain error for a title the
        library does not hold."""
        row = self._library_row(kind, catalog_id)
        if row is None:
            raise MediaError(f"that {kind} is not in the library - request it first")
        return row

    def grab_release(
        self, kind, catalog_id, guid, indexer_id, season=None, episode=None
    ):
        """Hand one release from the app's own search to the app, which
        downloads and imports it as its own. The submission tracks it, scoped
        to what the release covers."""
        row = self._held(kind, catalog_id)
        client = self._client(kind)
        title = _clean_text(row.get("title")) or f"{kind} {catalog_id}"
        scope, baselines = self._scoped(kind, row, season, episode)
        try:
            client.post("release", {"guid": str(guid), "indexerId": int(indexer_id)})
        except MediaError as e:
            if "HTTP 404" in str(e):
                # The app keeps search results for half an hour; after that the
                # guid means nothing to it.
                raise MediaError(
                    "that release is no longer in the app's search results - run "
                    "search_releases again and grab from the new list"
                ) from e
            raise
        return self._submission(
            kind,
            row["id"],
            title,
            catalog_id,
            phase="grabbed",
            work_id=json.dumps(["release", str(guid), int(indexer_id)]),
            detail=f"{client.name} accepted the release and handed it to the "
            "download client",
            **scope,
            **baselines,
        )

    def search_again(self, kind, catalog_id, season=None, episode=None):
        """A fresh search for a held title: the whole of it, one season, or
        one episode. What it promises is the search: the operation ends when
        the app's search has run and anything it took has imported."""
        row = self._held(kind, catalog_id)
        title = _clean_text(row.get("title")) or f"{kind} {catalog_id}"
        if kind == "series" and season is not None:
            scope, baselines = self._scoped(kind, row, season, episode)
            # A season is searched as a season (season packs count); one
            # episode as that episode. The scope watched is the same either
            # way: the episodes the search is for.
            command_ids = (
                self._search(kind, row["id"], episode_ids=scope["episode_ids"])
                if episode is not None
                else self._search(kind, row["id"], seasons=[int(season)])
            )
        else:
            scope, baselines = {}, self._baselines(kind, row)
            command_ids = self._search(kind, row["id"])
        return self._submission(
            kind,
            row["id"],
            title,
            catalog_id,
            command_ids=command_ids,
            phase="searching",
            promise="search",
            work_id=f"command:{','.join(str(i) for i in command_ids)}",
            detail=f"{self._client(kind).name} is searching again",
            **scope,
            **baselines,
        )

    def import_candidates(self, kind, download_id):
        """What the app would import for a finished download, by its own match
        and verdict: the files it matched and accepts, the ones it could not
        match, and the ones it rejects (a sample, not an upgrade), with the
        title the matched files belong to."""
        client = self._client(kind)
        rows = client.get(
            "manualimport", {"downloadId": download_id, "filterExistingFiles": "true"}
        )
        files: list[dict] = []
        unmatched: list = []
        rejected: list = []
        parent = None
        seasons: set[int] = set()
        for c in rows or []:
            if not isinstance(c, dict):
                continue
            name = c.get("relativePath") or c.get("path")
            reasons = [
                str(r.get("reason") or r) for r in (c.get("rejections") or []) if r
            ]
            if reasons:
                rejected.append({"file": name, "why": reasons[:3]})
                continue
            entry: dict = {
                "path": c.get("path"),
                "quality": c.get("quality"),
                "languages": c.get("languages") or [],
                "downloadId": download_id,
            }
            owner = c.get("movie" if kind == "movie" else "series") or {}
            episodes = c.get("episodes") or []
            if not owner.get("id") or (kind == "series" and not episodes):
                unmatched.append(name)
                continue
            if kind == "movie":
                entry["movieId"] = owner["id"]
            else:
                entry["seriesId"] = owner["id"]
                entry["episodeIds"] = [e.get("id") for e in episodes if e.get("id")]
                seasons.update(
                    int(e["seasonNumber"])
                    for e in episodes
                    if e.get("seasonNumber") is not None
                )
            parent = parent or owner
            files.append(entry)
        return {
            "files": files,
            "unmatched": unmatched,
            "rejected": rejected,
            "parent": parent,
            "seasons": sorted(n for n in seasons if n > 0) or None,
        }

    def manual_import(self, kind, download_id, candidates):
        """Import the matched files of `import_candidates`; the submission
        tracks the import against exactly the episodes (or the movie) those
        files are for."""
        parent = candidates["parent"] or {}
        client = self._client(kind)
        spec = _kind(kind)
        row = self._one(
            client.get(f"{spec['resource']}/{int(parent['id'])}"),
            client.name,
            spec["resource"],
        )
        title = _clean_text(row.get("title")) or f"{kind} {parent['id']}"
        scope: dict = {}
        if kind == "series":
            scope["episode_ids"] = sorted(
                {
                    int(i)
                    for entry in candidates["files"]
                    for i in entry.get("episodeIds") or []
                }
            )
        baselines = self._baselines(kind, row, episode_ids=scope.get("episode_ids"))
        command = self._one(
            client.post(
                "command",
                {
                    "name": "ManualImport",
                    "files": candidates["files"],
                    "importMode": "auto",
                },
            ),
            client.name,
            "import command",
        )
        return self._submission(
            kind,
            row["id"],
            title,
            row.get(spec["id_key"]),
            command_ids=[int(command["id"])],
            phase="importing",
            work_id=f"command:{command['id']}",
            detail=f"{client.name} is importing {len(candidates['files'])} file(s)",
            **scope,
            **baselines,
        )

    def observe(self, operation):
        external_ref = int(operation["external_ref"])
        metadata = operation.get("metadata") or {}
        phase = (operation.get("progress") or {}).get("phase")
        promise = metadata.get("promise", "acquire")
        if self._operation_kind(operation) == "movie":
            return self.observe_movie(
                external_ref,
                metadata.get("baseline_file_id"),
                metadata.get("command_ids"),
                phase,
                promise=promise,
            )
        return self.observe_series(
            external_ref,
            metadata.get("seasons"),
            baseline_episode_files=metadata.get("baseline_episode_files"),
            command_ids=metadata.get("command_ids"),
            previous_phase=phase,
            episode_ids=metadata.get("episode_ids"),
            promise=promise,
            episodes=metadata.get("episodes"),
        )
