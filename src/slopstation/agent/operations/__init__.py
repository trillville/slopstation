"""The operation ledger: persist and correlate work performed by external
services.

`monitors` reconciles the open rows with Steam, Radarr and Sonarr, and
`python -m slopstation.agent.operations` lists or settles them by hand."""

import time
import uuid
from typing import Any, TypedDict

from slopstation import paths, statefile


def operations_file():
    return paths.state("operations.json")


POLL_S = 30

QUEUED = "QUEUED"
RUNNING = "RUNNING"
UNKNOWN = "UNKNOWN"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELED = "CANCELED"
ACTIVE = {QUEUED, RUNNING, UNKNOWN}
TERMINAL = {SUCCEEDED, FAILED, CANCELED}


# Keys track() copies from a MediaService._submission result into the row's
# metadata. The other keys are the receipt and the row's own columns.
METADATA_KEYS = (
    "catalog_id",
    "preset",
    "profile",
    "seasons",
    "all_seasons",
    "baseline_file_id",
    "baseline_episode_files",
    "search_pending",
    "command_ids",
    "episode_ids",
    "episodes",
    "promise",
    "scope_label",
)


class Notification(TypedDict, total=False):
    """One spoken heads-up about an operation, keyed so it is said once."""

    operation_id: str
    key: str
    summary: str
    pending: bool
    created: int
    delivered: int | None


class OperationRow(TypedDict, total=False):
    """One row of operations.json. Keys after `delivered` are optional: old
    rows lack them, and a row is never rejected for what it lacks. Unknown keys
    survive every write."""

    id: str
    turn: str | None
    kind: str
    authority: str
    external_ref: str
    title: str
    state: str  # ACTIVE or TERMINAL, or one a newer build wrote
    # What the server said, plus "phase": searching, waiting_for_match,
    # grabbed, downloading, importing, ready in the order an acquisition moves
    # through them; a search-only promise ends in searched or search_failed.
    progress: dict[str, Any]
    detail: str
    created: int
    updated: int
    last_observed: int | None
    finished: int | None
    announcement_pending: bool
    delivered: int | None
    summary: str
    metadata: dict[str, Any]
    work_id: str
    notifications: list[Notification]


def _summary(operation, state):
    title = operation["title"]
    kind = operation.get("kind")
    phase = (operation.get("progress") or {}).get("phase")
    authority = str(operation.get("authority", "")).title()
    # A search promised a search, not a file: say what it found.
    if state == SUCCEEDED and phase == "searched":
        gained = (operation.get("progress") or {}).get("episodes", 0)
        if gained:
            noun = "episode" if gained == 1 else "episodes"
            return (
                f"{authority} searched again for {title}; "
                f"{gained} {noun} gained a file."
            )
        return f"{authority} searched again for {title} and found nothing better."
    if state == FAILED and phase == "search_failed":
        return f"{authority}'s search for {title} failed."
    if state == SUCCEEDED:
        if kind == "movie_acquisition":
            return f"{title} is ready to watch."
        if kind == "series_acquisition":
            return f"The requested episodes of {title} are ready to watch."
        return f"{title} finished installing."
    if state == CANCELED:
        if kind in ("movie_acquisition", "series_acquisition"):
            return f"The {title} media request was canceled."
        return f"The {title} install was canceled."
    if kind in ("movie_acquisition", "series_acquisition"):
        return f"The {title} media request failed."
    return f"The {title} install failed."


class OperationStore:
    """Store tracked operations in a local JSON file."""

    def __init__(self, log, on_terminal=None, on_notification=None):
        self.log = log
        self.on_terminal = on_terminal
        self.on_notification = on_notification
        self.path = operations_file()
        # A ledger that cannot be read is refused here and the file left as it
        # is.
        with statefile.guard(self.path):
            _read(self.path)

    def all(self) -> list[OperationRow]:
        with statefile.guard(self.path):
            return [r.copy() for r in _read(self.path)]

    def recent(self, limit=10) -> list[OperationRow]:
        rows = self.all()
        rows.sort(key=lambda r: r.get("updated", 0), reverse=True)
        return rows[:limit]

    def active(self, kind=None) -> list[OperationRow]:
        return [
            r
            for r in self.all()
            if r.get("state") in ACTIVE and (kind is None or r.get("kind") == kind)
        ]

    def get(self, operation_id) -> OperationRow | None:
        return next((r for r in self.all() if r.get("id") == operation_id), None)

    def update_metadata(
        self, operation_id, updates=None, remove=()
    ) -> OperationRow | None:
        now = int(time.time())
        with statefile.guard(self.path):
            rows = _read(self.path)
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            metadata = dict(row.get("metadata") or {})
            metadata.update(updates or {})
            for key in remove:
                metadata.pop(key, None)
            if metadata != row.get("metadata", {}):
                row.update({"metadata": metadata, "updated": now})
                statefile.write(self.path, rows)
            return row.copy()

    def track_external(
        self,
        kind,
        authority,
        external_ref,
        title,
        turn=None,
        state=RUNNING,
        detail="external authority accepted the request",
        metadata=None,
        observed=True,
        work_id=None,
    ):
        """Deduplicate a receipt for the same work, not every action on a title.

        Requests and Steam installs retain their resource identity. A manual
        grab, search or import supplies its release or command identity.
        """
        external_ref = str(external_ref)
        now = int(time.time())
        reused: OperationRow | None = None
        previous = None
        with statefile.guard(self.path):
            rows = _read(self.path)
            existing = next(
                (
                    r
                    for r in rows
                    if r.get("kind") == kind
                    and r.get("external_ref") == external_ref
                    and r.get("work_id") == work_id
                    and r.get("state") in ACTIVE
                ),
                None,
            )
            if existing is not None:
                updates: OperationRow = {}
                if existing.get("state") != state:
                    previous = existing["state"]
                    updates.update({"state": state, "detail": detail})
                if metadata is not None and existing.get("metadata") != metadata:
                    updates["metadata"] = metadata
                if updates:
                    updates["updated"] = now
                    existing.update(updates)
                    if observed:
                        existing["last_observed"] = now
                    statefile.write(self.path, rows)
                reused = existing.copy()
            else:
                created: OperationRow = {
                    "id": "op-" + uuid.uuid4().hex[:12],
                    "turn": turn,
                    "kind": kind,
                    "authority": authority,
                    "external_ref": external_ref,
                    "title": title,
                    "state": state,
                    "progress": {},
                    "detail": detail,
                    "created": now,
                    "updated": now,
                    "last_observed": now if observed else None,
                    "finished": None,
                    "announcement_pending": False,
                    "delivered": None,
                }
                if metadata is not None:
                    created["metadata"] = metadata
                if work_id is not None:
                    created["work_id"] = work_id
                rows.append(created)
                statefile.write(self.path, rows)
        if reused is not None:
            if previous is not None:
                self.log(
                    "operation_observed",
                    operation=reused["id"],
                    previous=previous,
                    state=state,
                    progress=reused.get("progress", {}),
                    detail=reused["detail"],
                )
            return reused
        self.log(
            "operation_created",
            operation=created["id"],
            turn=turn,
            kind=created["kind"],
            authority=created["authority"],
            external_ref=external_ref,
            state=created["state"],
        )
        return dict(created)

    def track_steam_install(self, appid, title, turn=None, verified=False):
        return self.track_external(
            "steam_install",
            "steam",
            str(int(appid)),
            title,
            turn=turn,
            state=RUNNING if verified else QUEUED,
            detail=(
                "Steam verified the install queue"
                if verified
                else "Steam accepted the install; verification is pending"
            ),
            observed=verified,
        )

    def observe(
        self, operation_id, state, progress=None, detail="", summary=None, announce=True
    ) -> OperationRow | None:
        """Persist one authority observation and fire on the first terminal edge."""
        now = int(time.time())
        terminal: OperationRow | None = None
        with statefile.guard(self.path):
            rows = _read(self.path)
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            if row.get("state") in TERMINAL:
                return row.copy()
            previous = row.get("state")
            progress = progress or {}
            changed = (
                previous != state
                or row.get("progress", {}) != progress
                or row.get("detail", "") != detail
            )
            row["last_observed"] = now
            if changed:
                row.update(
                    {
                        "state": state,
                        "progress": progress,
                        "detail": detail,
                        "updated": now,
                    }
                )
            if state in TERMINAL and previous != state:
                row.update(
                    {
                        "finished": now,
                        "announcement_pending": announce,
                        "summary": summary or _summary(row, state),
                    }
                )
                if announce:
                    terminal = row.copy()
            statefile.write(self.path, rows)
            out = row.copy()
        if changed:
            self.log(
                "operation_observed",
                operation=operation_id,
                previous=previous,
                state=state,
                progress=progress,
                detail=detail,
            )
        if terminal is not None and self.on_terminal is not None:
            try:
                self.on_terminal(terminal)
            except Exception as e:
                self.log.error(
                    "operation_announce_hook_failed", operation=operation_id, err=str(e)
                )
        return out

    def pending_announcements(self) -> list[OperationRow]:
        return [
            r
            for r in self.all()
            if r.get("state") in TERMINAL and r.get("announcement_pending")
        ]

    def notify(self, operation_id, key, summary) -> Notification | None:
        now = int(time.time())
        with statefile.guard(self.path):
            rows = _read(self.path)
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            notifications = list(row.get("notifications") or [])
            if any(item.get("key") == key for item in notifications):
                return None
            notification: Notification = {
                "operation_id": operation_id,
                "key": key,
                "summary": summary,
                "pending": True,
                "created": now,
                "delivered": None,
            }
            notifications.append(notification)
            row.update({"notifications": notifications, "updated": now})
            statefile.write(self.path, rows)
        self.log("operation_notification", operation=operation_id, key=key)
        if self.on_notification is not None:
            try:
                self.on_notification(notification.copy())
            except Exception as e:
                self.log.error(
                    "operation_announce_hook_failed", operation=operation_id, err=str(e)
                )
        return notification.copy()

    def pending_notifications(self) -> list[Notification]:
        return [
            item.copy()
            for row in self.all()
            for item in row.get("notifications") or []
            if item.get("pending")
        ]

    def mark_notification_delivered(self, operation_id, key) -> bool:
        now = int(time.time())
        with statefile.guard(self.path):
            rows = _read(self.path)
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return False
            changed = False
            for item in row.get("notifications") or []:
                if item.get("key") == key and item.get("pending"):
                    item.update({"pending": False, "delivered": now})
                    changed = True
            if changed:
                row["updated"] = now
                statefile.write(self.path, rows)
            return changed

    def mark_delivered(self, operation_id) -> bool:
        now = int(time.time())
        with statefile.guard(self.path):
            rows = _read(self.path)
            for row in rows:
                if row.get("id") == operation_id:
                    row.update(
                        {
                            "announcement_pending": False,
                            "delivered": now,
                            "updated": now,
                        }
                    )
                    statefile.write(self.path, rows)
                    return True
        return False

    def for_assistant(
        self, scope="active", limit=10, offset=0, acknowledge=False
    ) -> tuple[list[dict], int]:
        """One page of the scope's rows as the model reads them, and the
        scope's total. Only the rows on the page are acknowledged: a bulletin
        on a page nobody heard stays pending."""
        rows = self.active() if scope == "active" else self.recent(offset + limit)
        total = len(rows) if scope == "active" else len(self.all())
        rows = rows[offset : offset + limit]
        if acknowledge:
            for row in rows:
                if row.get("state") in TERMINAL and row.get("announcement_pending"):
                    self.mark_delivered(row["id"])
        return [
            {
                **{
                    k: r.get(k)
                    for k in (
                        "id",
                        "kind",
                        "title",
                        "state",
                        "progress",
                        "detail",
                        "created",
                        "updated",
                        "finished",
                    )
                },
                "scope": {
                    k: r.get("metadata", {})[k]
                    for k in ("seasons", "episode_ids", "scope_label", "promise")
                    if k in r.get("metadata", {})
                },
            }
            for r in rows
        ], total


def track(store, submission: dict, turn=None) -> dict:
    """Record one accepted external submission. The mutation already happened,
    so a failed local write reports itself and never invites a second one."""
    if store is None or submission.get("already_available"):
        return dict(submission)
    phase = submission.get("phase") or "searching"
    authority = str(submission["authority"]).title()
    detail = (
        submission.get("detail") or f"{authority} accepted the request and is searching"
    )
    metadata = {k: submission[k] for k in METADATA_KEYS if k in submission}
    try:
        operation = store.track_external(
            submission["kind"],
            submission["authority"],
            submission["external_ref"],
            submission["title"],
            turn=turn,
            detail=f"{authority} accepted the request",
            metadata=metadata,
            work_id=submission.get("work_id"),
        )
        operation = store.observe(operation["id"], RUNNING, {"phase": phase}, detail)
        return {**submission, "operation_id": operation["id"], "phase": phase}
    except Exception as e:
        store.log.error("tool_error", tool="track_media", err=str(e))
        return {**submission, "tracking": "failed"}


def _pairs(value):
    return {(int(s), int(e)) for s, e in value or ()}


def _pairs_left(pairs, seasons, deleted_pairs):
    """The (season, episode) pairs a delete of `seasons` and `deleted_pairs`
    leaves behind: the one rule for both covering and trimming a request."""
    return [
        pair
        for pair in pairs
        if int(pair[0]) not in seasons
        and (int(pair[0]), int(pair[1])) not in deleted_pairs
    ]


def covered_by_delete(
    store,
    kind,
    catalog_id,
    seasons=None,
    all_seasons=False,
    episode_ids=(),
    episodes=None,
):
    """The active acquisitions a delete of this scope would cover, with the
    search commands to cancel alongside them. `kind` is "movie" or "series".
    A movie is covered outright; a series only when the delete's scope holds
    every season its request asked for, so a partial delete leaves the
    request tracking the seasons it still owns. `episode_ids` names the
    episodes the service resolved for this deletion before changing state.
    A request whose episode ids Sonarr could not name yet is covered by the
    seasons its episodes belong to, or by the pairs in `episodes`: leaving it
    active is what would let the pending search start the download again once
    Sonarr catches up."""
    rows: list[dict] = []
    command_ids: list = []
    deleted_seasons = set(seasons or [])
    deleted_pairs = _pairs(episodes)
    for operation in (
        store.active(kind=f"{kind}_acquisition") if store is not None else []
    ):
        metadata = operation.get("metadata") or {}
        if int(metadata.get("catalog_id", 0) or 0) != int(catalog_id):
            continue
        requested = metadata.get("seasons")
        explicit = metadata.get("episode_ids")
        pending = metadata.get("episodes")
        if not (
            kind == "movie"
            or all_seasons
            or (
                set(explicit) <= set(episode_ids)
                if explicit is not None
                else not _pairs_left(pending, deleted_seasons, deleted_pairs)
                if pending
                else requested is not None and set(requested) <= deleted_seasons
            )
        ):
            continue
        rows.append(operation)
        command_ids.extend(metadata.get("command_ids") or [])
    return rows, command_ids


def record_canceled(store, operation, detail):
    """Close one operation the user asked to stop. Delivered on the spot:
    they are in the conversation that cancelled it."""
    store.observe(operation["id"], CANCELED, operation.get("progress", {}), detail)
    store.mark_delivered(operation["id"])


def record_deleted(store, rows, result, episodes=None):
    """Close the operations a completed delete covered, so the ledger stops
    announcing work whose files are gone. Called after the delete returns:
    the mutation already happened, and these rows describe it. `episodes`
    is the delete's own (season, episode) scope, for a request whose ids
    Sonarr has not named yet."""
    for operation in rows:
        record_canceled(store, operation, "the media request was deleted cleanly")
    if rows:
        result["operations_canceled"] = [row["id"] for row in rows]
    # A partial delete trims what it took from the requests it did not cover;
    # what is left keeps its own completion rule.
    if store is not None and "episode_ids" in result:
        deleted = set(result["episode_ids"])
        seasons = {int(n) for n in result.get("seasons") or []}
        deleted_pairs = _pairs(episodes)
        for operation in store.active("series_acquisition"):
            metadata = operation.get("metadata") or {}
            if metadata.get("catalog_id") != result.get("catalog_id"):
                continue
            ids = metadata.get("episode_ids") or []
            pairs = metadata.get("episodes") or []
            ids_left = sorted(set(ids) - deleted)
            pairs_left = _pairs_left(pairs, seasons, deleted_pairs)
            if len(ids_left) == len(ids) and len(pairs_left) == len(pairs):
                continue
            # The ids are the scope once Sonarr has named them, the pairs
            # until then; a request holding both is trimmed on both.
            if not (ids_left if ids else pairs_left):
                record_canceled(
                    store, operation, "the media request was deleted cleanly"
                )
                continue
            update: dict[str, Any] = {}
            if ids:
                update["episode_ids"] = ids_left
                update["scope_label"] = f"{len(ids_left)} selected episodes"
            if pairs:
                update["episodes"] = pairs_left
                update["scope_label"] = "episodes " + ", ".join(
                    f"S{int(s):02d}E{int(e):02d}" for s, e in pairs_left
                )
            store.update_metadata(operation["id"], update)
    return result


def _read(path) -> list[OperationRow]:
    """The ledger's rows. ValueError when the file cannot be read or is not a
    list of rows; an absent file is an empty ledger."""
    rows = statefile.load_strict(path, [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError(f"{path.name} is not a list of operations")
    return rows


def ledger_summary() -> dict | None:
    """Row counts for the doctor, or None when nothing was ever recorded.
    ValueError when the file cannot be read."""
    path = operations_file()
    if not path.exists():
        return None
    rows = _read(path)
    active = [r for r in rows if r.get("state") in ACTIVE]
    return {
        "recorded": len(rows),
        "active": len(active),
        "unknown": sum(r.get("state") == UNKNOWN for r in active),
        "pending": sum(bool(r.get("announcement_pending")) for r in rows),
    }


def owned_seasons() -> dict:
    """series id -> seasons an active series operation owns; None means the
    whole series. An unreadable ledger owns nothing, so a reader over-reports,
    which is the safe direction."""
    owned: dict = {}
    rows = statefile.load(operations_file(), [])
    for row in rows if isinstance(rows, list) else []:
        if row.get("kind") != "series_acquisition" or row.get("state") not in ACTIVE:
            continue
        seasons = (row.get("metadata") or {}).get("seasons")
        key = str(row.get("external_ref"))
        if seasons is None or owned.get(key, ()) is None:
            owned[key] = None
        else:
            owned.setdefault(key, set()).update(int(n) for n in seasons)
    return owned
