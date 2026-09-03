"""Radarr/Sonarr acquisitions that failed or stalled, found by polling."""

import threading

from slopstation.agent.tools.media_clients import MediaError, _clean_text

# Servarr history eventTypes that mean a grab did not become a file.
FAILURE_EVENTS = frozenset(("downloadFailed", "importFailed", "importBlocked"))
GRAB_EVENT = "grabbed"
# The history field naming the library row a grab belongs to, per app.
GRAB_REF = {"Sonarr": "seriesId", "Radarr": "movieId"}
HEALTH_POLL_S = 300


def _history_id(row):
    try:
        return int(row.get("id"))
    except (AttributeError, TypeError, ValueError):
        return -1


def _queue_detail(row):
    messages = []
    for entry in row.get("statusMessages") or ():
        if isinstance(entry, dict):
            for message in entry.get("messages") or ():
                messages.append(_clean_text(message, 80))
    if not messages:
        messages.append(_clean_text(row.get("errorMessage"), 80))
    return _clean_text("; ".join(message for message in messages if message))


class MediaHealthMonitor:
    """Report Radarr/Sonarr trouble nobody is sitting in front of.

    Polls rather than taking webhooks: a notification connection lives only in
    the container's config database, which is not in the checkout and does not
    survive a rebuilt config volume.
    """

    PAGE_SIZE = 50

    def __init__(self, clients, log, poll_s=HEALTH_POLL_S, operations=None):
        self.clients = tuple(clients)
        self.log = log
        self.poll_s = poll_s
        # Without the ledger a grab cannot be attributed, so that row stays
        # silent rather than calling everything unattributed.
        self.operations = operations
        self._stop = threading.Event()
        self._issues = {}
        self._history_id = {}
        self._stalled = {}
        self._last_failure = {}

    def start(self):
        threading.Thread(
            target=self._run, daemon=True, name="media-health-monitor"
        ).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self.reconcile_once()
            self._stop.wait(self.poll_s)

    def reconcile_once(self):
        for client in self.clients:
            try:
                self._health(client)
                self._history(client)
                self._queue(client)
                self._last_failure[client.name] = None
            except Exception as e:
                detail = _clean_text(e)
                # Unchanged failures stay silent; an unreachable app would
                # otherwise be one line per poll until someone noticed.
                if detail != self._last_failure.get(client.name):
                    self.log.error("media_watch_failed", app=client.name, err=detail)
                    self._last_failure[client.name] = detail

    def _health(self, client):
        rows = client.get("health")
        if not isinstance(rows, list):
            raise MediaError(f"{client.name} returned an invalid health report")
        current = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            source = _clean_text(row.get("source"), 60)
            if source and source not in current:
                current[source] = (
                    _clean_text(row.get("type"), 20).lower(),
                    _clean_text(row.get("message")),
                )
        seen = self._issues.get(client.name)
        for source, (kind, detail) in sorted(current.items()):
            # seen is None on the first pass: a health issue is current state,
            # not backlog, so startup reports what is wrong right now.
            if seen is not None and source in seen:
                continue
            # Bound as `log`, not `report`: _events_scan reads emitters from
            # source, and only the log call shapes are visible to it.
            log = self.log.error if kind == "error" else self.log.warn
            log(
                "media_health_issue",
                app=client.name,
                source=source,
                kind=kind,
                detail=detail,
            )
        for source in sorted(seen or ()):
            if source not in current:
                self.log.info("media_health_cleared", app=client.name, source=source)
        self._issues[client.name] = set(current)

    def _history(self, client):
        page = client.get(
            "history",
            {
                "pageSize": self.PAGE_SIZE,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        records = page.get("records") if isinstance(page, dict) else None
        if not isinstance(records, list):
            raise MediaError(f"{client.name} returned an invalid history page")
        watermark = self._history_id.get(client.name)
        newest = watermark
        failures: dict = {}
        grabs: dict = {}
        for row in sorted(records, key=_history_id):
            row_id = _history_id(row)
            if row_id < 0:
                continue
            if newest is None or row_id > newest:
                newest = row_id
            # The first pass only takes the watermark. Replaying whatever the
            # history still holds would make every restart look like an outage.
            if watermark is None or row_id <= watermark:
                continue
            kind = _clean_text(row.get("eventType"), 40)
            if kind == GRAB_EVENT:
                # Same collapse as failures: a season pack grabs once per
                # episode and is one line to a human.
                key = _clean_text(row.get("downloadId"), 60) or str(row_id)
                entry = grabs.get(key)
                if entry is None:
                    grabs[key] = {
                        "ref": _clean_text(row.get(GRAB_REF.get(client.name, "")), 20),
                        "title": _clean_text(row.get("sourceTitle"), 120),
                        "indexer": _clean_text(
                            (row.get("data") or {}).get("indexer")
                            if isinstance(row.get("data"), dict)
                            else None,
                            60,
                        ),
                        "records": 1,
                    }
                else:
                    entry["records"] += 1
                continue
            if kind not in FAILURE_EVENTS:
                continue
            data = row.get("data")
            # A season pack fails once per episode. Collapsing on the download
            # makes one bad grab one line; `records` keeps the fan-out visible.
            key = _clean_text(row.get("downloadId"), 60) or str(row_id)
            entry = failures.get(key)
            if entry is None:
                failures[key] = {
                    "kind": kind,
                    "title": _clean_text(row.get("sourceTitle"), 120),
                    "err": _clean_text(
                        (data or {}).get("message") if isinstance(data, dict) else None
                    ),
                    "records": 1,
                }
            else:
                entry["records"] += 1
        for entry in failures.values():
            self.log.error(
                "media_import_failed",
                app=client.name,
                kind=entry["kind"],
                title=entry["title"],
                err=entry["err"],
                records=entry["records"],
            )
        for entry in self._unattributed(client, grabs).values():
            # INFO, not warn: a monitored season legitimately keeps grabbing
            # new episodes long after the operation that requested it closed.
            # This is the audit trail for a grab the ledger cannot explain -
            # the only record that a release nobody asked for arrived.
            self.log.info(
                "media_grab_unattributed",
                app=client.name,
                title=entry["title"],
                indexer=entry["indexer"],
                records=entry["records"],
            )
        # An empty history still has to leave a watermark, or the first
        # failure to ever land would be skipped as backlog.
        self._history_id[client.name] = 0 if newest is None else newest

    def _unattributed(self, client, grabs):
        """The grabs no active operation accounts for. Attribution is by
        library row, not season: the question is whether anything asked for
        this title at all."""
        if self.operations is None or not grabs:
            return {}
        authority = client.name.casefold()
        owned = set()
        for operation in self.operations.active():
            if not isinstance(operation, dict):
                continue
            if _clean_text(operation.get("authority"), 20).casefold() != authority:
                continue
            ref = _clean_text(operation.get("external_ref"), 20)
            if ref:
                owned.add(ref)
        return {
            key: entry
            for key, entry in grabs.items()
            if entry["ref"] and entry["ref"] not in owned
        }

    def _queue(self, client):
        page = client.get("queue", {"pageSize": self.PAGE_SIZE})
        records = page.get("records") if isinstance(page, dict) else None
        if not isinstance(records, list):
            raise MediaError(f"{client.name} returned an invalid queue page")
        current = {}
        for row in records:
            if not isinstance(row, dict):
                continue
            status = _clean_text(row.get("trackedDownloadStatus"), 20).lower()
            if status not in ("warning", "error"):
                continue
            # A season pack is one queue record per episode. Keying on the
            # download collapses it to the one line a human would act on.
            key = _clean_text(row.get("downloadId") or row.get("id"), 60)
            if key and key not in current:
                current[key] = (
                    status,
                    _clean_text(row.get("title"), 120),
                    _queue_detail(row),
                )
        seen = self._stalled.get(client.name) or {}
        for key, (status, title, detail) in sorted(current.items()):
            if seen.get(key) == status:
                continue
            self.log.warn(
                "media_queue_stalled",
                app=client.name,
                download=key,
                status=status,
                title=title,
                err=detail,
            )
        self._stalled[client.name] = {key: value[0] for key, value in current.items()}
