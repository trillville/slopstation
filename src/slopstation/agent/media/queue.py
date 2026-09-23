"""The download queue and the commands Radarr and Sonarr run."""

from slopstation.agent.media.clients import (
    MediaError,
)
from slopstation.agent.media.core import _Core


def _command(client, command_id):
    """The command row, or None once the authority has dropped it."""
    try:
        row = client.get(f"command/{command_id}")
    except MediaError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    return row if isinstance(row, dict) else None


class _Queue(_Core):
    """Queue records, command state, and removing a download."""

    # -- what the arr apps hold, keyed by download ------------------------------

    def download_index(self, strict=False):
        """{infohash lower: {kind, title, queue_id, authority}} for every queue
        item Radarr and Sonarr are waiting on. This is the torrent-to-media
        link: a torrent in here belongs to an arr app, which owns its identity,
        location and lifecycle. With strict, a queue that cannot be read
        raises, because a decision that hangs on the link (deleting) must not
        treat an unread queue as an empty one."""
        out = {}
        for kind, client, id_key, include in (
            ("movie", self.radarr, "movie", "includeMovie"),
            ("series", self.sonarr, "series", "includeSeries"),
        ):
            try:
                queue = client.get(
                    "queue", {"page": 1, "pageSize": 1000, include: "true"}
                )
            except MediaError as e:
                self.log.warn("queue_read_failed", authority=client.name, err=str(e))
                if strict:
                    raise MediaError(f"{client.name}'s queue could not be read") from e
                continue
            for row in queue.get("records", []) if isinstance(queue, dict) else []:
                download_id = str(row.get("downloadId") or "").lower()
                if not download_id:
                    continue
                parent = row.get(id_key) if isinstance(row.get(id_key), dict) else {}
                out[download_id] = {
                    "kind": kind,
                    "title": parent.get("title") or f"{kind} {row.get(id_key + 'Id')}",
                    "queue_id": row.get("id"),
                    "authority": client.name,
                    "status": row.get("status"),
                }
        return out

    def download_known(self, download_id):
        """Whether either arr app's history has ever seen this infohash. Asked
        per hash with the history filter, because a page of recent history
        would miss an old import still seeding under the app's control."""
        for client in (self.radarr, self.sonarr):
            history = client.get(
                "history", {"page": 1, "pageSize": 1, "downloadId": download_id}
            )
            rows = history.get("records", []) if isinstance(history, dict) else []
            if rows:
                return True
        return False

    def arr_files(self):
        """Host-independent container paths of every file Radarr and Sonarr
        hold, from the movie rows (movieFile.path) and each series' episode
        files. Raises when an app cannot be read: a partial index would let a
        held file look deletable."""
        paths = set()
        for row in self.radarr.get("movie") or []:
            if isinstance(row, dict) and isinstance(row.get("movieFile"), dict):
                path = row["movieFile"].get("path")
                if path:
                    paths.add(str(path))
        for series in self.sonarr.get("series") or []:
            if not isinstance(series, dict) or "id" not in series:
                continue
            for f in self.sonarr.get("episodefile", {"seriesId": series["id"]}) or []:
                if isinstance(f, dict) and f.get("path"):
                    paths.add(str(f["path"]))
        return paths

    @staticmethod
    def _command_phase(client, command_ids):
        statuses = []
        for command_id in command_ids or []:
            row = _command(client, int(command_id))
            if row is None:
                continue
            status = str(row.get("status", "")).lower()
            result = str(row.get("result", "")).lower()
            if status in ("failed", "aborted", "cancelled", "orphaned"):
                raise MediaError(f"{client.name} search {status}")
            if status == "completed" and result == "unsuccessful":
                raise MediaError(f"{client.name} search failed")
            statuses.append(status)
        if any(status in ("queued", "started") for status in statuses):
            return "searching"
        return "waiting_for_match"

    def _idle_phase(self, client, command_ids, previous_phase):
        """The phase when nothing of the title's is in the queue: a download
        that was there is now importing; a release just grabbed has not
        reached the client yet; otherwise the search commands say."""
        if previous_phase == "downloading":
            return "importing"
        if previous_phase == "grabbed":
            return "grabbed"
        return self._command_phase(client, command_ids)

    @staticmethod
    def _queue_records(client, id_key, wanted_id):
        queue = client.get("queue", {"page": 1, "pageSize": 1000})
        if not isinstance(queue, dict):
            return []
        records = []
        for row in queue.get("records", []):
            if not isinstance(row, dict):
                continue
            try:
                matches = int(row.get(id_key, 0) or 0) == wanted_id
            except (TypeError, ValueError):
                matches = False
            if matches:
                records.append(row)
        return records

    @staticmethod
    def _queue_progress(records):
        downloads: dict = {}
        for index, row in enumerate(records):
            key = str(row.get("downloadId") or f"row-{index}")
            current = downloads.get(key)
            size = float(row.get("size", 0) or 0)
            left = float(row.get("sizeleft", 0) or 0)
            if current is None or size > current[0]:
                downloads[key] = (size, left)
        size = sum(row[0] for row in downloads.values())
        left = sum(row[1] for row in downloads.values())
        if size <= 0:
            return None
        return max(0, min(100, round((size - left) * 100 / size)))

    QUEUE_DELETE_PARAMS = {
        "removeFromClient": True,
        "blocklist": False,
        "skipRedownload": True,
        "changeCategory": False,
    }

    def _post_command(self, client, body):
        """Start one command in the app; its id, to watch."""
        command = self._one(client.post("command", body), client.name, "command")
        return int(command["id"])

    @staticmethod
    def _cancel_commands(client, command_ids):
        """Recall the searches still queued; count the started ones.

        The app answers 409 to cancelling a started command, and a finished
        one is nothing to cancel. Neither is an error: unmonitoring is what
        stops a running search."""
        canceled = 0
        running = 0
        for command_id in sorted({int(value) for value in command_ids or []}):
            status = str((_command(client, command_id) or {}).get("status", "")).lower()
            if status == "started":
                running += 1
                continue
            if status != "queued":
                continue
            try:
                client.delete(f"command/{command_id}")
            except MediaError as e:
                # It started between the read and the delete.
                if "HTTP 409" not in str(e):
                    raise
                running += 1
                continue
            canceled += 1
        return {"canceled": canceled, "running": running}

    @staticmethod
    def _download_key(row):
        """One download: a season pack has a queue row per episode."""
        return str(row.get("downloadId") or f"queue-{row.get('id')}")

    def _remove_queue(self, client, records):
        seen = set()
        removed = 0
        for row in records:
            key = self._download_key(row)
            if key in seen:
                continue
            seen.add(key)
            client.delete(f"queue/{int(row['id'])}", self.QUEUE_DELETE_PARAMS)
            removed += 1
        return removed
