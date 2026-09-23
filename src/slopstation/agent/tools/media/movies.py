"""Movie requests: submit, observe, delete."""

from slopstation.agent.tools.media.core import Observation
from slopstation.agent.tools.media.queue import _Queue
from slopstation.agent.tools.media_clients import (
    MediaError,
    _clean_text,
)
from slopstation.agent.tools.operations import (
    FAILED,
    RUNNING,
    SUCCEEDED,
)


class _Movies(_Queue):
    """Radarr operations."""

    def request_movie(self, tmdb_id, preset="default"):
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError) as e:
            raise MediaError("tmdb_id must be an integer") from e
        if tmdb_id <= 0:
            raise MediaError("tmdb_id must be positive")
        profile_id, profile_name = self._profile("movie", preset)
        existing = self._library_row("movie", tmdb_id)

        if existing is not None:
            movie = dict(existing)
            movie_id = int(movie["id"])
            title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
            try:
                current_profile_id = int(movie.get("qualityProfileId", 0))
            except (TypeError, ValueError):
                current_profile_id = 0
            if movie.get("hasFile") and current_profile_id == profile_id:
                return self._submission(
                    "movie", movie_id, title, tmdb_id, preset, profile_name, True
                )
            baseline_file_id = (
                self._movie_file_id(movie_id) if movie.get("hasFile") else None
            )
            movie.update(qualityProfileId=profile_id, monitored=True)
            self.radarr.put(f"movie/{movie_id}", movie)
        else:
            candidate = self._one(
                self.radarr.get("movie/lookup/tmdb", {"tmdbId": tmdb_id}),
                "Radarr",
                "movie lookup",
            )
            payload = dict(candidate)
            payload.pop("id", None)
            payload.update(
                rootFolderPath=self.cfg["movieRoot"],
                qualityProfileId=profile_id,
                monitored=True,
                minimumAvailability="released",
                addOptions={"searchForMovie": False, "addMethod": "manual"},
            )
            movie = self._one(
                self.radarr.post("movie", payload), "Radarr", "created movie"
            )
            movie_id = int(movie["id"])
            title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
            baseline_file_id = None
        body = {"name": "MoviesSearch", "movieIds": [movie_id]}
        command_ids = [self._post_command(self.radarr, body)]
        return self._submission(
            "movie",
            movie_id,
            title,
            tmdb_id,
            preset,
            profile_name,
            False,
            baseline_file_id=baseline_file_id,
            command_ids=command_ids,
        )

    def observe_movie(
        self,
        movie_id,
        baseline_file_id=None,
        command_ids=None,
        previous_phase=None,
        promise="acquire",
    ):
        movie = self._one(self.radarr.get(f"movie/{int(movie_id)}"), "Radarr", "movie")
        if movie.get("hasFile"):
            if baseline_file_id is None:
                return Observation(
                    SUCCEEDED,
                    {"phase": "ready", "percent": 100},
                    "Radarr reports the movie imported",
                )
            if self._movie_file_id(int(movie_id)) != int(baseline_file_id):
                return Observation(
                    SUCCEEDED,
                    {"phase": "ready", "percent": 100},
                    "Radarr imported the requested movie upgrade",
                )
        records = self._queue_records(self.radarr, "movieId", int(movie_id))
        percent = self._queue_progress(records)
        if records:
            progress = {"phase": "downloading"}
            if percent is not None:
                progress["percent"] = percent
            detail = (
                f"download is {percent}% complete"
                if percent is not None
                else "the movie download is active"
            )
        else:
            try:
                phase = self._idle_phase(self.radarr, command_ids, previous_phase)
            except MediaError as e:
                if promise != "search":
                    raise
                return Observation(FAILED, {"phase": "search_failed"}, str(e))
            if promise == "search" and phase not in ("searching", "importing"):
                # The search ran and the client holds nothing for it: that
                # is the promise, kept; the old file is what it found.
                return Observation(
                    SUCCEEDED,
                    {"phase": "searched"},
                    "Radarr searched and found nothing better than the file it holds",
                )
            progress = {"phase": phase}
            detail = (
                "Radarr is importing the requested movie file"
                if phase == "importing"
                else "Radarr handed the release to the download client and is "
                "waiting for it to appear"
                if phase == "grabbed"
                else "Radarr is searching for an acceptable movie release"
                if phase == "searching"
                else "no acceptable movie release is available yet; Radarr is watching"
            )
        return Observation(RUNNING, progress, detail)

    def delete_movie(self, tmdb_id, command_ids=None):
        tmdb_id = int(tmdb_id)
        movie = self._library_row("movie", tmdb_id)
        if movie is None:
            return {
                "ok": True,
                "kind": "movie",
                "catalog_id": tmdb_id,
                "removed": False,
                "detail": "the movie was not managed by Radarr",
            }
        movie_id = int(movie["id"])
        title = _clean_text(movie.get("title")) or f"TMDB {tmdb_id}"
        unmonitored = dict(movie)
        unmonitored["monitored"] = False
        self.radarr.put(f"movie/{movie_id}", unmonitored)
        self._cancel_commands(self.radarr, command_ids)
        queued = self._queue_records(self.radarr, "movieId", movie_id)
        downloads = self._remove_queue(self.radarr, queued)
        had_file = bool(movie.get("hasFile"))
        self.radarr.delete(
            f"movie/{movie_id}", {"deleteFiles": True, "addImportExclusion": False}
        )
        return {
            "ok": True,
            "kind": "movie",
            "catalog_id": tmdb_id,
            "title": title,
            "removed": True,
            "downloads_canceled": downloads,
            "files_deleted": 1 if had_file else 0,
            "detail": f"removed {title} from Radarr and deleted its files",
        }
