"""Reconcile tracked operations with Steam, Radarr, and Sonarr."""

import time

from slopstation.agent.monitor import Monitor
from slopstation.agent.operations import (
    FAILED,
    POLL_S,
    RUNNING,
    SUCCEEDED,
    UNKNOWN,
)
from slopstation.agent.steam import library


def _waiting_summary(operation, progress):
    """Spoken heads-up when waiting begins; None when waiting is pre-air."""
    authority = str(operation.get("authority", "media")).title()
    title = operation["title"]
    if operation.get("kind") == "series_acquisition":
        total = int(progress.get("total_episodes", 0) or 0)
        if not total:
            return None
        missing = total - int(progress.get("episodes", 0) or 0)
        return (
            f"{authority} could not find {missing} of the {total} "
            f"requested episodes of {title}. It will keep watching for "
            "a day, then leave the missing episodes out."
        )
    return (
        f"{authority} could not find an acceptable release of {title}. "
        "It will keep watching for a day, then close the request."
    )


def _gave_up_summary(operation, result):
    title = operation["title"]
    if not result.get("have"):
        return f"No acceptable release of {title} was found; the request was closed."
    parts = ", ".join(
        f"season {row['season']} ({row['episodes']} episodes)"
        for row in result.get("missing") or []
    )
    return (
        f"{title} is ready except {parts}; no acceptable release was "
        "found, so those episodes were left out."
    )


def _fully_installed_appids():
    return {
        int(r["appid"])
        for r in library.fetch_installed_ssh()
        if int(r.get("state", 0)) & 4
    }


class SteamMonitor(Monitor):
    """Observe active Steam installs without owning their execution."""

    THREAD_NAME = "steam-operation-monitor"

    def __init__(self, store, steam, log):
        self.store = store
        self.steam = steam
        self.log = log
        self.poll_s = POLL_S

    def reconcile_once(self):
        operations = self.store.active(kind="steam_install")
        if not operations:
            return 0
        try:
            online = self.steam.client_online()
        except Exception as e:
            self._unknown(operations, f"Steam observation failed: {e}")
            return len(operations)
        if not online:
            self._unknown(operations, "the configured Steam client is offline")
            return len(operations)
        try:
            downloads = {int(r["appid"]): r for r in self.steam.download_status()}
        except Exception as e:
            self._unknown(operations, f"Steam download status failed: {e}")
            return len(operations)

        needs_manifest = [
            r
            for r in operations
            if (
                int(r["external_ref"]) not in downloads
                or downloads[int(r["external_ref"])].get("percent") == 100
            )
        ]
        installed = set()
        probe_error = None
        if needs_manifest:
            try:
                installed = _fully_installed_appids()
            except Exception as e:
                probe_error = str(e)

        for operation in operations:
            appid = int(operation["external_ref"])
            download = downloads.get(appid)
            if appid in installed:
                self.store.observe(
                    operation["id"],
                    SUCCEEDED,
                    {"percent": 100},
                    "the gaming PC reports the app fully installed",
                )
            elif download is not None:
                progress = {k: download.get(k) for k in ("percent", "paused", "queue")}
                pct = progress["percent"]
                detail = (
                    "download paused"
                    if progress["paused"]
                    else f"download is {pct}% complete"
                    if pct is not None
                    else "download is queued"
                )
                if pct == 100 and probe_error:
                    detail += f"; manifest check failed: {probe_error}"
                self.store.observe(operation["id"], RUNNING, progress, detail)
            else:
                detail = (
                    f"could not verify the gaming PC manifest: {probe_error}"
                    if probe_error
                    else "not downloading and not reported fully installed"
                )
                self.store.observe(operation["id"], UNKNOWN, {}, detail)
        return len(operations)

    def _unknown(self, operations, detail):
        for operation in operations:
            self.store.observe(operation["id"], UNKNOWN, {}, detail)


class MediaMonitor(Monitor):
    """Observe Radarr/Sonarr imports without seeing release-level data."""

    THREAD_NAME = "media-operation-monitor"
    KINDS = {"movie_acquisition", "series_acquisition"}
    SEARCH_RETRY_DELAYS_S = (5 * 60, 30 * 60, 2 * 60 * 60)
    WAITING_GIVE_UP_S = 24 * 60 * 60

    def __init__(self, store, media, log, poll_s=POLL_S):
        self.store = store
        self.media = media
        self.log = log
        self.poll_s = poll_s

    def _maybe_give_up(self, operation, progress, now):
        metadata = operation.get("metadata") or {}
        # Indexer trouble is the retry ladder's problem, not evidence that no
        # release exists - never close the request over it.
        if metadata.get("search_retry_pending") or metadata.get(
            "search_retry_exhausted"
        ):
            return
        # total_episodes == 0 means nothing requested has aired: waiting is
        # the correct state, indefinitely.
        if operation.get("kind") == "series_acquisition" and not int(
            progress.get("total_episodes", 0) or 0
        ):
            return
        since = int(metadata.get("waiting_since", 0) or 0)
        if not since:
            self.store.update_metadata(operation["id"], {"waiting_since": now})
            return
        if now - since < self.WAITING_GIVE_UP_S:
            return
        result = self.media.abandon_missing(operation)
        # The waiting_for_match notification already said this would happen
        # and when; a spoken announcement a day later would land with no
        # context, so the close is silent.
        self.store.observe(
            operation["id"],
            SUCCEEDED if result.get("have") else FAILED,
            progress,
            "no acceptable release appeared; unmonitored the missing scope",
            summary=_gave_up_summary(operation, result),
            announce=False,
        )

    def _schedule_search_retry(self, operation, now):
        metadata = operation.get("metadata") or {}
        if metadata.get("search_retry_pending"):
            return operation
        count = int(metadata.get("search_retry_count", 0) or 0)
        if count >= len(self.SEARCH_RETRY_DELAYS_S):
            return self.store.update_metadata(
                operation["id"], {"search_retry_exhausted": True}
            )
        try:
            available = self.media.search_available(operation)
        except Exception:
            available = False
        if available:
            return operation
        return self.store.update_metadata(
            operation["id"],
            {
                "search_retry_pending": True,
                "search_retry_after": now + self.SEARCH_RETRY_DELAYS_S[count],
            },
        )

    def _dispatch_search_retry(self, operation, now):
        metadata = operation.get("metadata") or {}
        # The first search has not been dispatched yet, so there is nothing to
        # retry - and a request whose scope Sonarr could not resolve yet would
        # retry as the whole series.
        if metadata.get("search_pending"):
            return operation
        if not metadata.get("search_retry_pending") or now < int(
            metadata.get("search_retry_after", 0) or 0
        ):
            return operation
        try:
            if not self.media.search_available(operation):
                return operation
        except Exception:
            return operation

        count = int(metadata.get("search_retry_count", 0) or 0) + 1
        try:
            command_ids = self.media.retry_search(operation)
        except Exception:
            updates = {"search_retry_count": count}
            remove: tuple[str, ...] = ()
            if count < len(self.SEARCH_RETRY_DELAYS_S):
                updates["search_retry_after"] = now + self.SEARCH_RETRY_DELAYS_S[count]
            else:
                updates["search_retry_exhausted"] = True
                remove = ("search_retry_pending", "search_retry_after")
            self.store.update_metadata(operation["id"], updates, remove=remove)
            raise

        operation = self.store.update_metadata(
            operation["id"],
            {
                "command_ids": command_ids,
                "search_retry_count": count,
            },
            remove=(
                "search_retry_pending",
                "search_retry_after",
                "search_retry_exhausted",
            ),
        )
        return self.store.observe(
            operation["id"],
            RUNNING,
            {"phase": "searching"},
            f"{str(operation.get('authority', 'media')).title()} recovered; "
            "retrying the search",
        )

    def reconcile_once(self, now=None):
        now = int(time.time()) if now is None else int(now)
        active = [r for r in self.store.active() if r.get("kind") in self.KINDS]
        for operation in active:
            try:
                dispatched = self.media.dispatch_pending_series_search(operation)
                if dispatched:
                    operation = self.store.update_metadata(
                        operation["id"], dispatched, remove=("search_pending",)
                    )
                operation = self._dispatch_search_retry(operation, now)
                observation = self.media.observe(operation)
                state = observation.state
                previous_phase = (operation.get("progress") or {}).get("phase")
                progress = observation.progress
                phase = progress.get("phase")
                if (
                    state == RUNNING
                    and phase == "waiting_for_match"
                    and previous_phase == "searching"
                ):
                    operation = self._schedule_search_retry(operation, now)
                self.store.observe(operation["id"], state, progress, observation.detail)
                if state == RUNNING and phase != previous_phase:
                    if phase == "downloading":
                        self.store.notify(
                            operation["id"],
                            "download_started",
                            f"{operation['title']} has started downloading.",
                        )
                    elif phase == "waiting_for_match":
                        summary = _waiting_summary(operation, progress)
                        if summary:
                            self.store.notify(
                                operation["id"], "waiting_for_match", summary
                            )
                if state == RUNNING and phase == "waiting_for_match":
                    self._maybe_give_up(operation, progress, now)
                elif (operation.get("metadata") or {}).get(
                    "waiting_since"
                ) and state == RUNNING:
                    self.store.update_metadata(
                        operation["id"], remove=("waiting_since",)
                    )
            except Exception as e:
                authority = str(operation.get("authority", "media")).title()
                previous_phase = (operation.get("progress") or {}).get("phase")
                if previous_phase == "searching":
                    operation = self._schedule_search_retry(operation, now)
                self.store.observe(
                    operation["id"], UNKNOWN, {}, f"{authority} observation failed: {e}"
                )
        return len(active)
