"""Proton's forwarded port: read the Windows client's log, and hold
qBittorrent's listening port to it."""

import datetime
import os
import re
import threading
from pathlib import Path
from typing import Any

from slopstation.agent.tools.media_clients import (
    MediaConfigurationError,
    MediaError,
    _clean_text,
)

PROTON_ACTIVE_STATUSES = {"PortMappingCommunication", "SleepingUntilRefresh"}
PROTON_INACTIVE_STATUSES = {"DestroyPortMappingCommunication", "Stopped", "Error"}
PROTON_LOG_MAX_AGE_S = 45
PROTON_STATUS_RE = re.compile(
    r"(?ms)^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)"
    r"[^\r\n]*Received PortForwarding Status '(?P<status>[^']+)'"
    r"(?P<detail>.*?)(?=^\d{4}-\d{2}-\d{2}T|\Z)"
)
PROTON_PORT_RE = re.compile(r"Port pair\s+\d+->(?P<port>\d+)")


def default_proton_log_path():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise MediaConfigurationError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "Proton" / "Proton VPN" / "Logs" / "client-logs.txt"


def read_proton_port_state(path=None, now=None, max_age_s=PROTON_LOG_MAX_AGE_S):
    """Read the latest state periodically emitted by Proton's Windows client."""
    source = Path(path) if path is not None else default_proton_log_path()
    backup = source.with_name(f"{source.stem}.1{source.suffix}")
    sources = [candidate for candidate in (backup, source) if candidate.is_file()]
    if not sources:
        return {
            "state": "missing",
            "status": None,
            "port": None,
            "observed_at": None,
            "age_s": None,
            "path": str(source),
        }

    latest: dict[str, Any] | None = None
    for candidate in sources:
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            raise MediaError("Proton client log is unreadable") from e
        for match in PROTON_STATUS_RE.finditer(text):
            try:
                observed = datetime.datetime.fromisoformat(
                    match.group("timestamp").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if latest is not None and observed < latest["observed"]:
                continue
            port_match = PROTON_PORT_RE.search(match.group("detail"))
            latest = {
                "status": match.group("status"),
                "port": int(port_match.group("port")) if port_match else None,
                "observed": observed,
                "path": candidate,
            }
    if latest is None:
        return {
            "state": "unknown",
            "status": None,
            "port": None,
            "observed_at": None,
            "age_s": None,
            "path": str(source),
        }

    current = now or datetime.datetime.now(datetime.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.UTC)
    age_s = (current - latest["observed"]).total_seconds()
    status = latest["status"]
    port = latest["port"]
    if age_s < -5 or age_s > max_age_s:
        state = "stale"
    elif status in PROTON_ACTIVE_STATUSES and port is not None:
        state = "active"
    elif status in PROTON_INACTIVE_STATUSES:
        state = "inactive"
    else:
        state = "transitional"
    return {
        "state": state,
        "status": status,
        "port": port,
        "observed_at": latest["observed"].isoformat().replace("+00:00", "Z"),
        "age_s": round(age_s, 3),
        "path": str(latest["path"]),
    }


class ProtonPortMonitor:
    """Synchronize a fresh Proton Windows mapping into qBittorrent."""

    def __init__(self, client, log, path=None, poll_s=30, now=None):
        self.client = client
        self.log = log
        self.path = Path(path) if path is not None else default_proton_log_path()
        self.poll_s = poll_s
        self.now = now
        self._stop = threading.Event()
        self._last_failure = None

    def inspect(self):
        return read_proton_port_state(self.path, now=self.now)

    def reconcile_once(self):
        source = self.inspect()
        result = {**source, "changed": False}
        if source["state"] == "missing":
            raise MediaError("Proton client log is missing")
        if source["state"] == "unknown":
            raise MediaError("Proton client log format is unrecognized")
        if source["state"] == "stale":
            raise MediaError("Proton port-forwarding state is stale")
        if source["state"] != "active":
            self._last_failure = None
            return result
        updated = self.client.set_listen_port(source["port"])
        result.update(updated)
        self._last_failure = None
        if updated["changed"]:
            self.log.info(
                "proton_port_synced",
                port=updated["listen_port"],
                previous_port=updated["previous_port"],
                source_age_s=source["age_s"],
            )
        return result

    def start(self):
        threading.Thread(
            target=self._run, daemon=True, name="proton-port-monitor"
        ).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except Exception as e:
                detail = _clean_text(e)
                if detail != self._last_failure:
                    self.log.error("proton_port_sync_failed", err=detail)
                    self._last_failure = detail
            self._stop.wait(self.poll_s)
