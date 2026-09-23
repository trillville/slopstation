"""What every part of the media service shares: the clients, the catalog lookups, and the receipt a submission returns."""

import dataclasses
from typing import Any

from slopstation.agent.media.clients import (
    MediaConfigurationError,
    MediaError,
    _clean_text,
    _kind,
)


@dataclasses.dataclass(frozen=True)
class Observation:
    """One look at Radarr or Sonarr for a tracked request. `state` is an
    operations state: RUNNING while the server still holds the work; SUCCEEDED,
    FAILED or CANCELED once it does not. `metadata_ready` is False while Sonarr
    is still listing the episodes a request covers."""

    state: str
    progress: dict[str, Any] = dataclasses.field(default_factory=dict)
    detail: str = ""
    metadata_ready: bool = True


class _Core:
    """Construction and the helpers every media operation uses."""

    def __init__(self, cfg, log, radarr, sonarr, prowlarr=None, qbit=None):
        self.cfg = cfg
        self.log = log
        self.radarr = radarr
        self.sonarr = sonarr
        # Optional: the tools that reach them are offered only when present.
        self.prowlarr = prowlarr
        self.qbit = qbit

    def _client(self, kind):
        return getattr(self, _kind(kind)["authority"])

    def _library_row(self, kind, catalog_id):
        """The service's own record for a catalog id, or None if it holds none."""
        spec = _kind(kind)
        client = self._client(kind)
        return self._existing(
            client.get(spec["resource"], {spec["id_key"]: catalog_id}),
            spec["id_key"],
            catalog_id,
            client.name,
        )

    def _catalog_title(self, kind, catalog_id):
        """The catalogue's own title for an id, or None if it names nothing."""
        spec = _kind(kind)
        client = self._client(kind)
        try:
            if kind == "movie":
                row = client.get("movie/lookup/tmdb", {"tmdbId": catalog_id})
                rows = [row] if isinstance(row, dict) else []
            else:
                rows = client.get("series/lookup", {"term": f"tvdb:{catalog_id}"})
            match = self._existing(rows, spec["id_key"], catalog_id, client.name)
        except MediaError:
            return None
        return None if match is None else _clean_text(match.get("title"))

    def _profile(self, kind, preset):
        preset = str(preset or "default").lower()
        mapping = self.cfg.get(_kind(kind)["presets_key"], {})
        if preset not in mapping:
            allowed = ", ".join(sorted(mapping)) or "none"
            raise MediaConfigurationError(
                f"unknown {kind} preset {preset}; configured presets: {allowed}"
            )
        wanted = str(mapping[preset])
        client = self._client(kind)
        rows = client.get("qualityprofile")
        if not isinstance(rows, list):
            raise MediaError(f"{client.name} returned invalid quality profiles")
        for row in rows:
            if (
                isinstance(row, dict)
                and str(row.get("name", "")).casefold() == wanted.casefold()
            ):
                try:
                    return int(row["id"]), wanted
                except (KeyError, TypeError, ValueError) as e:
                    raise MediaError(f"{client.name} profile has no id") from e
        raise MediaConfigurationError(
            f"{client.name} has no quality profile named {wanted}"
        )

    @staticmethod
    def _one(value, authority, resource):
        if not isinstance(value, dict):
            raise MediaError(f"{authority} returned an invalid {resource}")
        return value

    @staticmethod
    def _existing(value, catalog_key, catalog_id, authority):
        if not isinstance(value, list):
            raise MediaError(f"{authority} returned an invalid library response")
        for row in value:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get(catalog_key, 0) or 0) == catalog_id:
                    return row
            except (TypeError, ValueError):
                continue
        return None

    def _movie_file_id(self, movie_id):
        rows = self.radarr.get("moviefile", {"movieId": movie_id})
        if not isinstance(rows, list) or not rows:
            raise MediaError("Radarr reports a movie file but did not return it")
        try:
            return int(rows[0]["id"])
        except (KeyError, TypeError, ValueError) as e:
            raise MediaError("Radarr movie file has no id") from e

    @staticmethod
    def _operation_kind(operation):
        kind = operation.get("kind")
        if kind == "movie_acquisition":
            return "movie"
        if kind == "series_acquisition":
            return "series"
        raise MediaError(f"unsupported media operation kind {kind}")

    @staticmethod
    def _submission(
        kind,
        external_ref,
        title,
        catalog_id,
        preset=None,
        profile=None,
        already_available=False,
        seasons=None,
        baseline_file_id=None,
        baseline_episode_files=None,
        search_pending=False,
        command_ids=None,
        phase="searching",
        detail=None,
        episode_ids=None,
        promise="acquire",
        work_id=None,
        scope_label=None,
        episodes=None,
    ) -> dict:
        """What one accepted piece of work looks like to the operation store.
        A request carries its preset and profile and a season scope, or the
        (season, episode) pairs it asked for; work on a held title (a grab, a
        search, an import) carries the phase it starts in and, for a series,
        the exact episodes it covers. `promise` is what done means: media on
        disk for the scope, or a search run."""
        out: dict = {
            "ok": True,
            "kind": f"{kind}_acquisition",
            "authority": _kind(kind)["authority"],
            "external_ref": str(external_ref),
            "title": title,
            "catalog_id": catalog_id,
            "already_available": already_available,
            "phase": phase,
        }
        if preset is not None:
            out["preset"] = str(preset or "default").lower()
            out["profile"] = profile
        if detail is not None:
            out["detail"] = detail
        if kind == "series" and episode_ids is None and episodes is None:
            out["seasons"] = seasons
        if episode_ids is not None:
            out["episode_ids"] = list(episode_ids)
        if episodes is not None:
            out["episodes"] = [list(pair) for pair in episodes]
        if kind == "series":
            out["scope_label"] = scope_label or (
                f"{len(episode_ids)} selected episodes"
                if episode_ids is not None
                else "seasons " + ", ".join(str(n) for n in seasons)
                if seasons
                else "all regular seasons"
            )
        if promise != "acquire":
            out["promise"] = promise
        if work_id is not None:
            out["work_id"] = work_id
        if baseline_file_id is not None:
            out["baseline_file_id"] = baseline_file_id
        if baseline_episode_files is not None:
            out["baseline_episode_files"] = baseline_episode_files
        if search_pending:
            out["search_pending"] = True
        if command_ids:
            out["command_ids"] = command_ids
        return out
