"""Durable correlation and observation for externally-owned work."""
import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cglib
import library

OPERATIONS_FILE = cglib.STATE / "operations.json"
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
    if state == SUCCEEDED:
        return f"{title} finished installing."
    if state == CANCELED:
        return f"The {title} install was canceled."
    return f"The {title} install failed."


class OperationStore:
    """One K15-owned JSON ledger; external systems remain authoritative."""

    def __init__(self, log, on_terminal=None, path=None):
        self.log = log
        self.on_terminal = on_terminal
        self.path = path or OPERATIONS_FILE
        self._lock = threading.Lock()

    def _load(self):
        rows = cglib.load_json(self.path, [])
        return rows if isinstance(rows, list) else []

    def _save(self, rows):
        cglib.write_json(self.path, rows)

    def all(self):
        with self._lock:
            return [dict(r) for r in self._load()]

    def recent(self, limit=10):
        rows = self.all()
        rows.sort(key=lambda r: r.get("updated", 0), reverse=True)
        return rows[:limit]

    def active(self, kind=None):
        return [r for r in self.all()
                if r.get("state") in ACTIVE
                and (kind is None or r.get("kind") == kind)]

    def get(self, operation_id):
        return next((r for r in self.all() if r.get("id") == operation_id), None)

    def track_steam_install(self, appid, title, turn=None, verified=False):
        """Create one tracker per active appid, or reuse the existing one."""
        external_ref = str(int(appid))
        now = int(time.time())
        created = None
        reused = None
        previous = None
        with self._lock:
            rows = self._load()
            existing = next((r for r in rows
                             if r.get("kind") == "steam_install"
                             and r.get("external_ref") == external_ref
                             and r.get("state") in ACTIVE), None)
            if existing is not None:
                if verified and existing["state"] != RUNNING:
                    previous = existing["state"]
                    existing.update(state=RUNNING, updated=now,
                                    last_observed=now,
                                    detail="Steam verified the install queue")
                    self._save(rows)
                reused = dict(existing)
            else:
                state = RUNNING if verified else QUEUED
                created = {
                    "id": "op-" + uuid.uuid4().hex[:12],
                    "turn": turn,
                    "kind": "steam_install",
                    "authority": "steam",
                    "external_ref": external_ref,
                    "title": title,
                    "state": state,
                    "progress": {},
                    "detail": ("Steam verified the install queue" if verified
                               else "Steam accepted the install; "
                                    "verification is pending"),
                    "created": now,
                    "updated": now,
                    "last_observed": now if verified else None,
                    "finished": None,
                    "announcement_pending": False,
                    "delivered": None,
                }
                rows.append(created)
                self._save(rows)
        if reused is not None:
            if previous is not None:
                self.log("operation_observed", operation=reused["id"],
                         previous=previous, state=RUNNING,
                         progress=reused.get("progress", {}),
                         detail=reused["detail"], changed=True)
            return reused
        self.log("operation_created", operation=created["id"], turn=turn,
                 kind=created["kind"], authority=created["authority"],
                 external_ref=external_ref, state=created["state"])
        return dict(created)

    def observe(self, operation_id, state, progress=None, detail=""):
        """Persist one authority observation and fire on the first terminal edge."""
        if state not in STATES:
            raise ValueError(f"unknown operation state {state}")
        now = int(time.time())
        terminal = None
        changed = False
        previous = None
        out = None
        with self._lock:
            rows = self._load()
            row = next((r for r in rows if r.get("id") == operation_id), None)
            if row is None:
                return None
            if row.get("state") in TERMINAL:
                return dict(row)
            previous = row.get("state")
            progress = progress or {}
            changed = (previous != state or row.get("progress", {}) != progress
                       or row.get("detail", "") != detail)
            row["last_observed"] = now
            if changed:
                row.update(state=state, progress=progress, detail=detail,
                           updated=now)
            if state in TERMINAL and previous != state:
                row.update(finished=now, announcement_pending=True,
                           summary=_summary(row, state))
                terminal = dict(row)
            self._save(rows)
            out = dict(row)
        if changed:
            self.log("operation_observed", operation=operation_id,
                     previous=previous, state=state, progress=progress,
                     detail=detail, changed=True)
        if terminal is not None and self.on_terminal is not None:
            try:
                self.on_terminal(terminal)
            except Exception as e:
                self.log.error("operation_announce_hook_failed",
                               operation=operation_id, err=str(e))
        return out

    def pending_announcements(self):
        return [r for r in self.all()
                if r.get("state") in TERMINAL
                and r.get("announcement_pending")]

    def mark_delivered(self, operation_id):
        now = int(time.time())
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.get("id") == operation_id:
                    row.update(announcement_pending=False, delivered=now,
                               updated=now)
                    self._save(rows)
                    return True
        return False

    def cancel(self, operation_id):
        operation = self.get(operation_id)
        if operation is None:
            return False, f"no operation named {operation_id}"
        if operation.get("state") in TERMINAL:
            return False, f"{operation_id} is already {operation['state'].lower()}"
        self.log("operation_cancel_refused", operation=operation_id,
                 authority=operation.get("authority"))
        return False, ("Steam install cancellation is not supported; "
                       "the operation was left unchanged")

    def for_assistant(self, scope="active", limit=10, acknowledge=False):
        rows = self.active() if scope == "active" else self.recent(limit)
        if acknowledge:
            for row in rows:
                if (row.get("state") in TERMINAL
                        and row.get("announcement_pending")):
                    self.mark_delivered(row["id"])
        return [{k: r.get(k) for k in (
                    "id", "kind", "title", "state", "progress", "detail",
                    "created", "updated", "finished")}
                for r in rows[:limit]]


def _fully_installed_appids():
    return {int(r["appid"]) for r in library.fetch_installed_ssh()
            if int(r.get("state", 0)) & 4}


class SteamMonitor:
    """Observe active Steam installs without owning their execution."""

    def __init__(self, store, steam, log, installed_probe=None, poll_s=POLL_S):
        self.store = store
        self.steam = steam
        self.log = log
        self.installed_probe = installed_probe or _fully_installed_appids
        self.poll_s = poll_s
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="steam-operation-monitor").start()

    def _run(self):
        while True:
            try:
                self.reconcile_once()
            except Exception as e:
                self.log.error("operation_monitor_failed", err=str(e))
            self._wake.wait(self.poll_s)
            self._wake.clear()

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

        missing = [r for r in operations
                   if int(r["external_ref"]) not in downloads]
        installed = set()
        probe_error = None
        if missing:
            try:
                installed = set(self.installed_probe())
            except Exception as e:
                probe_error = str(e)

        for operation in operations:
            appid = int(operation["external_ref"])
            download = downloads.get(appid)
            if download is not None:
                progress = {k: download.get(k) for k in
                            ("percent", "paused", "queue")}
                pct = progress["percent"]
                detail = ("download paused" if progress["paused"] else
                          f"download is {pct}% complete" if pct is not None else
                          "download is queued")
                self.store.observe(operation["id"], RUNNING, progress, detail)
            elif appid in installed:
                self.store.observe(operation["id"], SUCCEEDED, {"percent": 100},
                                   "the gaming PC reports the app fully installed")
            else:
                detail = (f"could not verify the gaming PC manifest: {probe_error}"
                          if probe_error else
                          "not downloading and not reported fully installed")
                self.store.observe(operation["id"], UNKNOWN, {}, detail)
        return len(operations)

    def _unknown(self, operations, detail):
        for operation in operations:
            self.store.observe(operation["id"], UNKNOWN, {}, detail)


def _line(operation):
    progress = operation.get("progress") or {}
    pct = progress.get("percent")
    suffix = f" ({pct}%)" if pct is not None else ""
    return (f"{operation['id']} {operation['state']} {operation['kind']} "
            f"{operation['title']}{suffix}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect durable operations")
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--active", action="store_true")
    show = sub.add_parser("show")
    show.add_argument("operation")
    sub.add_parser("reconcile")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("operation")
    args = parser.parse_args(argv)

    log = cglib.make_log("voice")
    store = OperationStore(log)
    if args.command == "list":
        rows = store.active() if args.active else store.recent(50)
        if not rows:
            print("no operations")
        for operation in rows:
            print(_line(operation))
        return 0
    if args.command == "show":
        operation = store.get(args.operation)
        if operation is None:
            print(f"no operation named {args.operation}")
            return 1
        print(json.dumps(operation, indent=2))
        return 0
    if args.command == "cancel":
        ok, detail = store.cancel(args.operation)
        print(detail)
        return 0 if ok else 1

    import steam_session
    cfg = cglib.config()
    steam = steam_session.SteamSession(
        cglib.load_secrets(), log, machine_name=cfg.get("steamMachineName"))
    if not steam.available():
        print("Steam account session is not enrolled")
        return 1
    count = SteamMonitor(store, steam, log).reconcile_once()
    print(f"reconciled {count} active Steam operation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
