"""Series requests and the season and episode arithmetic behind them."""

import datetime
from typing import Any

from slopstation.agent.media.clients import (
    MediaError,
    _clean_text,
    _parse_time,
)
from slopstation.agent.media.core import Observation
from slopstation.agent.media.queue import _Queue
from slopstation.agent.operations import (
    CANCELED,
    FAILED,
    RUNNING,
    SUCCEEDED,
)


class _Series(_Queue):
    """Sonarr operations and episode scopes."""

    def _monitor_episodes(self, episode_ids, monitored):
        if episode_ids:
            self.sonarr.put(
                "episode/monitor", {"episodeIds": episode_ids, "monitored": monitored}
            )

    @staticmethod
    def _seasons(value):
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise MediaError("seasons must be a non-empty list or omitted")
        try:
            seasons = sorted({int(n) for n in value})
        except (TypeError, ValueError) as e:
            raise MediaError("season numbers must be integers") from e
        if any(n <= 0 for n in seasons):
            raise MediaError("season numbers must be positive; specials are explicit")
        return seasons

    @staticmethod
    def _episodes(value):
        """Explicit episodes as sorted (season, episode) pairs, from the
        `{"season", "episode"}` objects the tool takes or the `[season,
        episode]` lists the operation store keeps."""
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise MediaError("episodes must be a non-empty list or omitted")
        pairs = set()
        for item in value:
            if isinstance(item, dict):
                item = (item.get("season"), item.get("episode"))
            try:
                season, episode = (int(n) for n in item)
            except (TypeError, ValueError) as e:
                raise MediaError(
                    "each episode needs a season and episode number"
                ) from e
            if season <= 0 or episode <= 0:
                raise MediaError("season and episode numbers must be positive")
            pairs.add((season, episode))
        return sorted(pairs)

    @staticmethod
    def _episode_ids_for(rows, episodes):
        """Sonarr's ids for (season, episode) pairs, and the pairs it has no
        row for yet."""
        if not isinstance(rows, list):
            raise MediaError("Sonarr returned invalid episodes")
        by_number = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                key = (int(row.get("seasonNumber", -1) or 0), int(row["episodeNumber"]))
                by_number[key] = int(row["id"])
            except (KeyError, TypeError, ValueError) as e:
                raise MediaError("Sonarr episode has no id") from e
        ids = sorted(by_number[pair] for pair in episodes if pair in by_number)
        missing = [pair for pair in episodes if pair not in by_number]
        return ids, missing

    @staticmethod
    def _episode_label(episodes):
        return "episodes " + ", ".join(f"S{s:02d}E{e:02d}" for s, e in episodes)

    def _set_series_seasons(self, series, selected):
        """`selected` None monitors every normal season; an empty list leaves
        the seasons as they are, for a request scoped to episodes.

        Seasons outside `selected` are left as they are. On a series that was
        already in the library they are somebody else's desired state -
        clearing them is what would stop a part-aired season from filling in
        as episodes air.
        """
        out = dict(series)
        seasons = []
        for season in out.get("seasons") or []:
            if not isinstance(season, dict):
                continue
            row = dict(season)
            number = int(row.get("seasonNumber", -1))
            if number > 0 and (selected is None or number in selected):
                row["monitored"] = True
            seasons.append(row)
        out.update(monitored=True, seasons=seasons)
        return out

    def _search_series(self, series_id, seasons):
        if seasons is None:
            body = {"name": "SeriesSearch", "seriesId": series_id}
            return [self._post_command(self.sonarr, body)]
        return [
            self._post_command(
                self.sonarr,
                {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season},
            )
            for season in seasons
        ]

    def _target_episodes(
        self, rows, seasons=None, now=None, monitored_only=True, episode_ids=None
    ):
        """The episodes a scope covers. Explicit episode ids are the scope
        whole - specials and unmonitored episodes included, since somebody
        chose them; a season scope is the aired, monitored episodes of the
        normal seasons (all of them when `seasons` is None)."""
        if not isinstance(rows, list):
            raise MediaError("Sonarr returned invalid episodes")
        if episode_ids:
            wanted = {int(i) for i in episode_ids}
            return [
                row
                for row in rows
                if isinstance(row, dict) and int(row.get("id", 0) or 0) in wanted
            ]
        seasons = self._seasons(seasons)
        now = now or datetime.datetime.now(datetime.UTC)
        targets = []
        for episode in rows:
            if not isinstance(episode, dict):
                continue
            number = int(episode.get("seasonNumber", 0) or 0)
            if number <= 0 or (seasons is not None and number not in seasons):
                continue
            if monitored_only and not episode.get("monitored"):
                continue
            aired = _parse_time(episode.get("airDateUtc"))
            if aired is None or aired > now:
                continue
            targets.append(episode)
        return targets

    def _missing_scope(self, operation):
        """The scope's rows with no file yet, and how many already have one."""
        if self._operation_kind(operation) == "movie":
            movie = self._one(
                self.radarr.get(f"movie/{int(operation['external_ref'])}"),
                "Radarr",
                "movie",
            )
            has_file = bool(movie.get("hasFile"))
            return [] if has_file else [movie], int(has_file)
        metadata = operation.get("metadata") or {}
        rows = self.sonarr.get("episode", {"seriesId": int(operation["external_ref"])})
        episode_ids = metadata.get("episode_ids")
        if episode_ids is None and metadata.get("episodes"):
            # Still pending: the pairs are the scope. Read as no scope at
            # all this would take every monitored episode of the series.
            episode_ids, _ = self._episode_ids_for(
                rows, self._episodes(metadata["episodes"])
            )
        if episode_ids is not None and not episode_ids:
            # An explicit scope that resolves to nothing covers nothing.
            # The fallback below is for a request with no episode scope.
            return [], 0
        targets = self._target_episodes(
            rows,
            self._seasons(metadata.get("seasons")),
            episode_ids=episode_ids,
        )
        missing = [row for row in targets if not row.get("hasFile")]
        return missing, len(targets) - len(missing)

    @staticmethod
    def _episode_ids(rows):
        ids = []
        for row in rows:
            try:
                ids.append(int(row["id"]))
            except (KeyError, TypeError, ValueError) as e:
                raise MediaError("Sonarr episode has no id") from e
        return sorted(ids)

    def _search(self, kind, row_id, seasons=None, episode_ids=None):
        """Start the app's search for one title: a movie, the given episodes,
        the given seasons, or the whole series. The command ids to watch."""
        if kind == "movie":
            body = {"name": "MoviesSearch", "movieIds": [int(row_id)]}
            return [self._post_command(self.radarr, body)]
        if episode_ids:
            body = {"name": "EpisodeSearch", "episodeIds": sorted(episode_ids)}
            return [self._post_command(self.sonarr, body)]
        return self._search_series(int(row_id), seasons)

    def _baselines(self, kind, row, seasons=None, episode_ids=None, rows=None):
        """The file ids on disk before new work starts, for exactly the scope
        of that work, so a later observation does not call an upgrade done
        because the old file is still there, and does not wait on episodes
        the work never touched."""
        if kind == "movie":
            return {
                "baseline_file_id": self._movie_file_id(int(row["id"]))
                if row.get("hasFile")
                else None
            }
        if rows is None:
            rows = self.sonarr.get("episode", {"seriesId": int(row["id"])})
        files = {}
        for episode in self._target_episodes(
            rows, seasons, monitored_only=False, episode_ids=episode_ids
        ):
            if not episode.get("hasFile"):
                continue
            try:
                files[str(int(episode["id"]))] = int(episode["episodeFileId"])
            except (KeyError, TypeError, ValueError) as e:
                raise MediaError("Sonarr episode file has no id") from e
        return {"baseline_episode_files": files}

    def _episode_scope(self, series_id, season, episode=None):
        """The episode ids one release covers: one episode, or a whole season
        (a season pack), specials included. With the episode rows, so the
        baselines need no second read."""
        rows = self.sonarr.get("episode", {"seriesId": int(series_id)})
        if not isinstance(rows, list):
            raise MediaError("Sonarr returned invalid episodes")
        ids = self._episode_ids(
            row
            for row in rows
            if isinstance(row, dict)
            and int(row.get("seasonNumber", -1) or 0) == int(season)
            and (
                episode is None
                or int(row.get("episodeNumber", -1) or 0) == int(episode)
            )
        )
        if not ids:
            what = f"season {season}" + (f" episode {episode}" if episode else "")
            raise MediaError(f"Sonarr has no {what} for this series")
        return ids, rows

    def _scoped(self, kind, row, season=None, episode=None):
        """The scope one piece of work on a held title covers, and its
        baselines: for a series that is the episodes of the season (or the
        one episode) it was searched under."""
        if kind == "movie":
            return {}, self._baselines(kind, row)
        if season is None:
            raise MediaError("pass the season the release was searched under")
        episode_ids, rows = self._episode_scope(row["id"], season, episode)
        return (
            {
                "episode_ids": episode_ids,
                "scope_label": f"season {season}"
                + (f", episode {episode}" if episode is not None else ""),
            },
            self._baselines(kind, row, episode_ids=episode_ids, rows=rows),
        )

    @staticmethod
    def _episode_metadata_ready(rows, seasons):
        if not isinstance(rows, list) or not rows:
            return False
        wanted = set(seasons or [])
        available = {
            int(row.get("seasonNumber", 0) or 0)
            for row in rows
            if isinstance(row, dict)
        }
        return bool(available - {0}) if not wanted else wanted <= available

    def _apply_series_monitoring(self, series_id, seasons):
        """Write the monitored state Slopstation asked for, once Sonarr has
        finished adding the series.

        Sonarr acts on the `monitor` add option in a pass of its own that runs
        after the create call has returned, and that pass rewrites both the
        series flag and the season flags. Writing during it is silently lost,
        and a series Sonarr believes is unmonitored is one its own searches
        refuse with "Series is not monitored" - the request then waits out its
        day having never been searchable. Sonarr clears `addOptions` when that
        pass is done, so that is the signal to write; until then, wait.
        """
        series = self._one(self.sonarr.get(f"series/{series_id}"), "Sonarr", "series")
        if series.get("addOptions"):
            return False
        # The add option left every season unmonitored, so turning the
        # asked-for ones on leaves only those monitored; on a series that was
        # in the library the other seasons are somebody else's desired state.
        self.sonarr.put(
            f"series/{series_id}", self._set_series_seasons(series, seasons)
        )
        return True

    def _monitor_series_episodes(self, rows, seasons):
        wanted = set(seasons or [])
        unmonitored = [
            row
            for row in rows
            if isinstance(row, dict)
            and int(row.get("seasonNumber", 0) or 0) > 0
            and (not wanted or int(row.get("seasonNumber", 0) or 0) in wanted)
            and not row.get("monitored")
        ]
        self._monitor_episodes(self._episode_ids(unmonitored), True)

    def dispatch_pending_series_search(self, operation):
        """Start the search a request left pending because Sonarr was still
        adding the series. The metadata to record with it (the command ids,
        and the episode ids an episode-scoped request could only resolve
        now), or False while Sonarr is not ready."""
        metadata = operation.get("metadata") or {}
        if operation.get("kind") != "series_acquisition" or not metadata.get(
            "search_pending"
        ):
            return False
        series_id = int(operation["external_ref"])
        seasons = self._seasons(metadata.get("seasons"))
        episodes = self._episodes(metadata.get("episodes"))
        rows = self.sonarr.get("episode", {"seriesId": series_id})
        if episodes is not None:
            episode_ids, missing = self._episode_ids_for(rows, episodes)
            if missing:
                return False
            if not self._apply_series_monitoring(series_id, []):
                return False
            self._monitor_episodes(episode_ids, True)
            return {
                "command_ids": self._search(
                    "series", series_id, episode_ids=episode_ids
                ),
                "episode_ids": episode_ids,
            }
        if not self._episode_metadata_ready(rows, seasons):
            return False
        if not self._apply_series_monitoring(series_id, seasons):
            return False
        self._monitor_series_episodes(rows, seasons)
        return {"command_ids": self._search_series(series_id, seasons)}

    def request_series(self, tvdb_id, preset="default", seasons=None, episodes=None):
        """Request seasons of a series, or exactly the given (season, episode)
        pairs. An episode scope monitors those episodes alone and searches
        for them one by one, so the seasons around them are never touched."""
        try:
            tvdb_id = int(tvdb_id)
        except (TypeError, ValueError) as e:
            raise MediaError("tvdb_id must be an integer") from e
        if tvdb_id <= 0:
            raise MediaError("tvdb_id must be positive")
        seasons = self._seasons(seasons)
        episodes = self._episodes(episodes)
        if seasons is not None and episodes is not None:
            raise MediaError("request seasons or episodes, not both")
        profile_id, profile_name = self._profile("series", preset)
        existing = self._library_row("series", tvdb_id)
        search_pending = False
        command_ids = []
        episode_ids = None
        scope_label = None if episodes is None else self._episode_label(episodes)

        if existing is not None:
            series = dict(existing)
            series_id = int(series["id"])
            title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
            try:
                profile_changed = int(series.get("qualityProfileId", 0)) != profile_id
            except (TypeError, ValueError):
                profile_changed = True
            baseline_episode_files = None
            rows = None
            if episodes is not None:
                rows = self.sonarr.get("episode", {"seriesId": series_id})
                episode_ids, missing = self._episode_ids_for(rows, episodes)
                if missing:
                    raise MediaError(
                        f"Sonarr has no {self._episode_label(missing)} for {title}"
                    )
            if profile_changed:
                baseline_episode_files = self._baselines(
                    "series", series, seasons, episode_ids=episode_ids, rows=rows
                )["baseline_episode_files"]
            series["qualityProfileId"] = profile_id
            series = self._set_series_seasons(
                series, [] if episodes is not None else seasons
            )
            self.sonarr.put(f"series/{series_id}", series)
            if episode_ids:
                self._monitor_episodes(episode_ids, True)
            observation = self.observe_series(
                series_id,
                seasons,
                baseline_episode_files=baseline_episode_files,
                episode_ids=episode_ids,
            )
            if observation.state == SUCCEEDED:
                return self._submission(
                    "series",
                    series_id,
                    title,
                    tvdb_id,
                    preset,
                    profile_name,
                    True,
                    seasons,
                    episode_ids=episode_ids,
                    episodes=episodes,
                    scope_label=scope_label,
                )
            if episode_ids:
                command_ids = self._search("series", series_id, episode_ids=episode_ids)
            elif observation.metadata_ready:
                command_ids = self._search_series(series_id, seasons)
            else:
                search_pending = True
        else:
            rows = self.sonarr.get("series/lookup", {"term": f"tvdb:{tvdb_id}"})
            candidate = self._existing(rows, "tvdbId", tvdb_id, "Sonarr")
            if candidate is None:
                raise MediaError(f"Sonarr could not resolve TVDB {tvdb_id}")
            payload = dict(candidate)
            payload.pop("id", None)
            payload.update(
                rootFolderPath=self.cfg["seriesRoot"],
                qualityProfileId=profile_id,
                seasonFolder=True,
                monitored=True,
                addOptions={
                    "monitor": "all"
                    if seasons is None and episodes is None
                    else "none",
                    "searchForMissingEpisodes": False,
                    "searchForCutoffUnmetEpisodes": False,
                },
            )
            series = self._one(
                self.sonarr.post("series", payload), "Sonarr", "created series"
            )
            series_id = int(series["id"])
            title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
            baseline_episode_files = None
            # The season scope is written by `_apply_series_monitoring` once
            # Sonarr has finished adding the series; writing it here would
            # land inside Sonarr's own post-add pass and be thrown away.
            search_pending = True
        return self._submission(
            "series",
            series_id,
            title,
            tvdb_id,
            preset,
            profile_name,
            False,
            seasons,
            baseline_episode_files=baseline_episode_files,
            search_pending=search_pending,
            command_ids=command_ids,
            episode_ids=episode_ids,
            episodes=episodes,
            scope_label=scope_label,
        )

    def observe_series(
        self,
        series_id,
        seasons=None,
        now=None,
        baseline_episode_files=None,
        command_ids=None,
        previous_phase=None,
        episode_ids=None,
        promise="acquire",
        episodes=None,
    ):
        rows = self.sonarr.get("episode", {"seriesId": int(series_id)})
        # Explicit episodes exist by construction; a season scope has to wait
        # for Sonarr to populate them. Unmonitoring cancels a season request;
        # an explicit episode was chosen unmonitored or not.
        if episodes is not None and not episode_ids:
            # An episode request on a series Sonarr is still adding: its ids
            # are resolved when the pending search is dispatched. Until every
            # asked-for episode has a row the scope is not readable, and the
            # rows that exist are unmonitored by design, not cancelled.
            episode_ids, missing = self._episode_ids_for(rows, self._episodes(episodes))
            if missing:
                return Observation(
                    RUNNING,
                    {
                        "episodes": 0,
                        "total_episodes": 0,
                        "percent": 0,
                        "phase": "searching",
                    },
                    "Sonarr is still populating episode metadata",
                    metadata_ready=False,
                )
        metadata_ready = bool(episode_ids) or self._episode_metadata_ready(
            rows, seasons
        )
        scope = (
            []
            if episode_ids
            else [
                row
                for row in rows
                if isinstance(row, dict)
                and int(row.get("seasonNumber", 0) or 0) > 0
                and (seasons is None or int(row.get("seasonNumber", 0) or 0) in seasons)
            ]
        )
        if metadata_ready and scope and not any(row.get("monitored") for row in scope):
            return Observation(
                CANCELED,
                {"episodes": 0, "total_episodes": 0, "percent": 0},
                "Sonarr reports the requested episodes are unmonitored",
            )
        targets = self._target_episodes(rows, seasons, now, episode_ids=episode_ids)
        baseline = baseline_episode_files or {}
        total = len(targets)
        ready = 0
        for episode in targets:
            if not episode.get("hasFile"):
                continue
            old_file_id = baseline.get(str(episode.get("id")))
            if old_file_id is not None:
                try:
                    if int(episode.get("episodeFileId", 0)) == int(old_file_id):
                        continue
                except (TypeError, ValueError):
                    continue
            ready += 1
        percent = round(ready * 100 / total) if total else 0
        progress: dict[str, Any] = {
            "episodes": ready,
            "total_episodes": total,
            "percent": percent,
        }
        if not metadata_ready:
            progress["phase"] = "searching"
            detail = "Sonarr is still populating episode metadata"
            complete = False
        elif not total:
            progress["phase"] = "waiting_for_match"
            detail = "no requested monitored episodes have aired yet"
            complete = False
        elif ready == total:
            progress["phase"] = "ready"
            detail = f"{ready} of {total} aired episodes are ready"
            complete = True
        else:
            queue = self._queue_records(self.sonarr, "seriesId", int(series_id))
            wanted_ids = {int(row["id"]) for row in targets if row.get("id")}
            queue = [
                row
                for row in queue
                if not row.get("episodeId")
                or int(row.get("episodeId", 0) or 0) in wanted_ids
            ]
            queue_percent = self._queue_progress(queue)
            if queue:
                progress["phase"] = "downloading"
                if queue_percent is not None:
                    progress["download_percent"] = queue_percent
                detail = (
                    f"download is {queue_percent}% complete; "
                    f"{ready} of {total} episodes are imported"
                    if queue_percent is not None
                    else f"download is active; {ready} of {total} episodes are imported"
                )
            else:
                try:
                    phase = self._idle_phase(self.sonarr, command_ids, previous_phase)
                except MediaError as e:
                    if promise != "search":
                        raise
                    progress["phase"] = "search_failed"
                    return Observation(
                        FAILED, progress, str(e), metadata_ready=metadata_ready
                    )
                if promise == "search" and phase not in ("searching", "importing"):
                    # The search ran and the client holds nothing for it:
                    # that is the promise, kept, whatever it found.
                    progress["phase"] = "searched"
                    return Observation(
                        SUCCEEDED,
                        progress,
                        f"Sonarr searched; {ready} of {total} episodes gained a file",
                        metadata_ready=metadata_ready,
                    )
                progress["phase"] = phase
                detail = (
                    f"Sonarr is importing episodes; {ready} of {total} are ready"
                    if phase == "importing"
                    else "Sonarr handed the release to the download client and is "
                    "waiting for it to appear"
                    if phase == "grabbed"
                    else "Sonarr is searching for acceptable episode releases"
                    if phase == "searching"
                    else "no acceptable episode release is available yet; Sonarr is watching"
                )
            complete = False
        return Observation(
            SUCCEEDED if complete else RUNNING,
            progress,
            detail,
            metadata_ready=metadata_ready,
        )

    def season_counts(self, series_id):
        """Per season of a Sonarr series: episodes held, aired but missing,
        and not yet aired, and whether any is monitored."""
        episodes = self.sonarr.get("episode", {"seriesId": series_id}) or []
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
            air = _parse_time(e.get("airDateUtc"))
            aired = air is not None and air <= now
            if e.get("hasFile"):
                s["held"] += 1
            elif aired:
                s["missing"] += 1
            else:
                s["upcoming"] += 1
            s["monitored"] = s["monitored"] or bool(e.get("monitored"))
        return [seasons[k] for k in sorted(seasons)]

    def episode_id(self, series_id, season, episode):
        """Sonarr's id for one episode of a series, or None when it has no row."""
        rows = self.sonarr.get("episode", {"seriesId": series_id}) or []
        ids, _ = self._episode_ids_for(rows, [(int(season), int(episode))])
        return ids[0] if ids else None

    def episodes_in_seasons(self, tvdb_id, seasons):
        """Resolve a deletion's episode scope while Sonarr still has its rows."""
        series = self._library_row("series", int(tvdb_id))
        if series is None:
            return []
        rows = self.sonarr.get("episode", {"seriesId": int(series["id"])})
        return sorted(
            int(row["id"])
            for row in rows
            if int(row.get("seasonNumber", 0) or 0) in seasons
        )

    def episodes_in_scope(self, tvdb_id, episodes):
        """Resolve requested (season, episode) pairs to Sonarr's ids, for a
        request whose ids were never resolved because Sonarr was still adding
        the series. The rows that exist by now; a pair Sonarr never populated
        has nothing to act on."""
        series = self._library_row("series", int(tvdb_id))
        if series is None:
            return []
        rows = self.sonarr.get("episode", {"seriesId": int(series["id"])})
        ids, _ = self._episode_ids_for(rows, self._episodes(episodes))
        return ids

    def delete_series(
        self,
        tvdb_id,
        seasons=None,
        all_seasons=False,
        command_ids=None,
        *,
        episode_ids=None,
    ):
        tvdb_id = int(tvdb_id)
        selected = self._seasons(seasons)
        if selected is None and not all_seasons and episode_ids is None:
            raise MediaError("series deletion needs seasons or explicit all_seasons")
        series = self._library_row("series", tvdb_id)
        if series is None:
            return {
                "ok": True,
                "kind": "series",
                "catalog_id": tvdb_id,
                "removed": False,
                "detail": "the series was not managed by Sonarr",
            }
        series_id = int(series["id"])
        title = _clean_text(series.get("title")) or f"TVDB {tvdb_id}"
        self._cancel_commands(self.sonarr, command_ids)
        if all_seasons:
            queued = self._queue_records(self.sonarr, "seriesId", series_id)
            downloads = self._remove_queue(self.sonarr, queued)
            self.sonarr.delete(
                f"series/{series_id}",
                {"deleteFiles": True, "addImportListExclusion": False},
            )
            return {
                "ok": True,
                "kind": "series",
                "catalog_id": tvdb_id,
                "title": title,
                "removed": True,
                "downloads_canceled": downloads,
                "all_seasons": True,
                "detail": f"removed all seasons of {title} from Sonarr and deleted their files",
            }

        rows = self.sonarr.get("episode", {"seriesId": series_id})
        if not isinstance(rows, list):
            raise MediaError("Sonarr returned invalid episodes")
        wanted = [
            row
            for row in rows
            if isinstance(row, dict)
            and (
                int(row.get("id", 0) or 0) in episode_ids
                if episode_ids is not None
                else int(row.get("seasonNumber", 0) or 0) in selected
            )
        ]
        if not wanted:
            # Sonarr has no row for any of it: nothing to erase.
            return {
                "ok": True,
                "kind": "series",
                "catalog_id": tvdb_id,
                "title": title,
                "removed": False,
                "seasons": selected,
                "episode_ids": [],
                "detail": f"Sonarr holds none of those episodes of {title}",
            }
        episode_ids = sorted({int(row["id"]) for row in wanted if row.get("id")})
        self._monitor_episodes(episode_ids, False)
        updated = dict(series)
        updated["seasons"] = [
            {**row, "monitored": False}
            if isinstance(row, dict)
            and int(row.get("seasonNumber", -1)) in (selected or [])
            else row
            for row in series.get("seasons") or []
        ]
        self.sonarr.put(f"series/{series_id}", updated)
        wanted_ids = set(episode_ids)
        queued = [
            row
            for row in self._queue_records(self.sonarr, "seriesId", series_id)
            if int(row.get("episodeId", 0) or 0) in wanted_ids
        ]
        downloads = self._remove_queue(self.sonarr, queued)
        file_ids = sorted(
            {
                int(row.get("episodeFileId", 0) or 0)
                for row in wanted
                if row.get("hasFile") and int(row.get("episodeFileId", 0) or 0) > 0
            }
        )
        for file_id in file_ids:
            self.sonarr.delete(f"episodefile/{file_id}")
        scope = (
            "season " + ", ".join(str(n) for n in selected)
            if selected is not None
            else f"{len(episode_ids)} selected episodes"
        )
        return {
            "ok": True,
            "kind": "series",
            "catalog_id": tvdb_id,
            "title": title,
            "removed": True,
            "seasons": selected,
            "downloads_canceled": downloads,
            "files_deleted": len(file_ids),
            "episode_ids": episode_ids,
            "detail": f"deleted {scope} of {title} and stopped monitoring it",
        }
