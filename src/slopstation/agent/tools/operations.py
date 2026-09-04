"""Persist and correlate work performed by external services."""

import time
import uuid
from typing import Any

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
STATES = ACTIVE | TERMINAL


def _summary(operation, state):
    title = operation["title"]
    kind = operation.get("kind")
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

    def _load(self):
        rows = statefile.load(self.path, [])
        return rows if isinstance(rows, list) else []

    def _save(self, rows):
        statefile.write(self.path, rows)

    def all(self):
        with statefile.guard(self.path):
            return [dict(r) for r in self._load()]

    def recent(self, limit=10):
        rows = self.all()
        rows.sort(key=lambda r: r.get("updated", 0), reverse=True)
        return rows[:limit]

    def active(self, kind=None):
        return [
            r
            for r in self.all()
            if r.get("state") in ACTIVE and (kind is None or r.get("kind") == kind)
        ]

    def get(self, operation_id):
        return next((r for r in self.all() if r.get("id") == operation_id), None)

    def update_metadata(self, operation_id, updates=None, remove=()):
        now = int(time.time())
        with statefile.guard(self.path):
            rows = self._load()
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            metadata = dict(row.get("metadata") or {})
            metadata.update(updates or {})
            for key in remove:
                metadata.pop(key, None)
            if metadata != row.get("metadata", {}):
                row.update(metadata=metadata, updated=now)
                self._save(rows)
            return dict(row)

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
    ):
        """Create one active tracker per concrete authority resource."""
        if state not in ACTIVE:
            raise ValueError(f"new operation state must be active, got {state}")
        external_ref = str(external_ref)
        now = int(time.time())
        created = None
        reused = None
        previous = None
        with statefile.guard(self.path):
            rows = self._load()
            existing = next(
                (
                    r
                    for r in rows
                    if r.get("kind") == kind
                    and r.get("external_ref") == external_ref
                    and r.get("state") in ACTIVE
                ),
                None,
            )
            if existing is not None:
                updates: dict[str, Any] = {}
                if existing.get("state") != state:
                    previous = existing["state"]
                    updates.update(state=state, detail=detail)
                if metadata is not None and existing.get("metadata") != metadata:
                    updates["metadata"] = metadata
                if updates:
                    existing.update(updates, updated=now)
                    if observed:
                        existing["last_observed"] = now
                    self._save(rows)
                reused = dict(existing)
            else:
                created = {
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
                rows.append(created)
                self._save(rows)
        if reused is not None:
            if previous is not None:
                self.log(
                    "operation_observed",
                    operation=reused["id"],
                    previous=previous,
                    state=state,
                    progress=reused.get("progress", {}),
                    detail=reused["detail"],
                    changed=True,
                )
            return reused
        assert created is not None
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
    ):
        """Persist one authority observation and fire on the first terminal edge."""
        if state not in STATES:
            raise ValueError(f"unknown operation state {state}")
        now = int(time.time())
        terminal = None
        changed = False
        previous = None
        out = None
        with statefile.guard(self.path):
            rows = self._load()
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            if row.get("state") in TERMINAL:
                return dict(row)
            previous = row.get("state")
            progress = progress or {}
            changed = (
                previous != state
                or row.get("progress", {}) != progress
                or row.get("detail", "") != detail
            )
            row["last_observed"] = now
            if changed:
                row.update(state=state, progress=progress, detail=detail, updated=now)
            if state in TERMINAL and previous != state:
                row.update(
                    finished=now,
                    announcement_pending=announce,
                    summary=summary or _summary(row, state),
                )
                if announce:
                    terminal = dict(row)
            self._save(rows)
            out = dict(row)
        if changed:
            self.log(
                "operation_observed",
                operation=operation_id,
                previous=previous,
                state=state,
                progress=progress,
                detail=detail,
                changed=True,
            )
        if terminal is not None and self.on_terminal is not None:
            try:
                self.on_terminal(terminal)
            except Exception as e:
                self.log.error(
                    "operation_announce_hook_failed", operation=operation_id, err=str(e)
                )
        return out

    def pending_announcements(self):
        return [
            r
            for r in self.all()
            if r.get("state") in TERMINAL and r.get("announcement_pending")
        ]

    def notify(self, operation_id, key, summary):
        now = int(time.time())
        notification = None
        with statefile.guard(self.path):
            rows = self._load()
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            notifications = list(row.get("notifications") or [])
            if any(item.get("key") == key for item in notifications):
                return None
            notification = {
                "operation_id": operation_id,
                "key": key,
                "summary": summary,
                "pending": True,
                "created": now,
                "delivered": None,
            }
            notifications.append(notification)
            row.update(notifications=notifications, updated=now)
            self._save(rows)
        self.log("operation_notification", operation=operation_id, key=key)
        if self.on_notification is not None:
            try:
                self.on_notification(dict(notification))
            except Exception as e:
                self.log.error(
                    "operation_announce_hook_failed", operation=operation_id, err=str(e)
                )
        return dict(notification)

    def pending_notifications(self):
        return [
            dict(item)
            for row in self.all()
            for item in row.get("notifications") or []
            if item.get("pending")
        ]

    def mark_notification_delivered(self, operation_id, key):
        now = int(time.time())
        with statefile.guard(self.path):
            rows = self._load()
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return False
            changed = False
            for item in row.get("notifications") or []:
                if item.get("key") == key and item.get("pending"):
                    item.update(pending=False, delivered=now)
                    changed = True
            if changed:
                row["updated"] = now
                self._save(rows)
            return changed

    def mark_delivered(self, operation_id):
        now = int(time.time())
        with statefile.guard(self.path):
            rows = self._load()
            for row in rows:
                if row.get("id") == operation_id:
                    row.update(announcement_pending=False, delivered=now, updated=now)
                    self._save(rows)
                    return True
        return False

    def for_assistant(self, scope="active", limit=10, acknowledge=False):
        rows = self.active() if scope == "active" else self.recent(limit)
        if acknowledge:
            for row in rows:
                if row.get("state") in TERMINAL and row.get("announcement_pending"):
                    self.mark_delivered(row["id"])
        return [
            {
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
            }
            for r in rows[:limit]
        ]


def track(store, submission, turn=None):
    """Record one accepted external submission. The mutation already happened,
    so a failed local write reports itself and never invites a second one."""
    if store is None or submission.get("already_available"):
        return submission
    metadata = {
        k: submission[k]
        for k in (
            "catalog_id",
            "preset",
            "profile",
            "seasons",
            "all_seasons",
            "baseline_file_id",
            "baseline_episode_files",
            "search_pending",
            "command_ids",
        )
        if k in submission
    }
    authority = str(submission["authority"]).title()
    try:
        operation = store.track_external(
            submission["kind"],
            submission["authority"],
            submission["external_ref"],
            submission["title"],
            turn=turn,
            detail=f"{authority} accepted the request",
            metadata=metadata,
        )
        operation = store.observe(
            operation["id"],
            RUNNING,
            {"phase": "searching"},
            f"{authority} accepted the request and is searching",
        )
        return {**submission, "operation_id": operation["id"], "phase": "searching"}
    except Exception as e:
        store.log.error("tool_error", tool="track_media", err=str(e))
        return {**submission, "tracking": "failed"}


def covered_by_delete(store, kind, catalog_id, seasons=None, all_seasons=False):
    """The active acquisitions a delete of this scope would cover, with the
    search commands to cancel alongside them. `kind` is "movie" or "series".
    A movie is covered outright; a series only when the delete's scope holds
    every season its request asked for, so a partial delete leaves the
    request tracking the seasons it still owns."""
    rows: list[dict] = []
    command_ids: list = []
    for operation in (
        store.active(kind=f"{kind}_acquisition") if store is not None else []
    ):
        metadata = operation.get("metadata") or {}
        if int(metadata.get("catalog_id", 0) or 0) != int(catalog_id):
            continue
        requested = metadata.get("seasons")
        if not (
            kind == "movie"
            or all_seasons
            or (requested is not None and set(requested) <= set(seasons or []))
        ):
            continue
        rows.append(operation)
        command_ids.extend(metadata.get("command_ids") or [])
    return rows, command_ids


def record_deleted(store, rows, result=None):
    """Close the operations a completed delete covered, so the ledger stops
    announcing work whose files are gone. Called after the delete returns:
    the mutation already happened, and these rows describe it."""
    for operation in rows:
        store.observe(
            operation["id"],
            CANCELED,
            operation.get("progress", {}),
            "the media request was deleted cleanly",
        )
        store.mark_delivered(operation["id"])
    if rows and result is not None:
        result["operations_canceled"] = [row["id"] for row in rows]
    return result


if __name__ == "__main__":
    from slopstation.agent.tools.operations_monitors import main

    raise SystemExit(main())
