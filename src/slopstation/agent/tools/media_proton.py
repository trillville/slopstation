"""Proton's forwarded port: read the Windows client's log, hold
qBittorrent's listening port to it, and keep its peer sockets alive."""

import datetime
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from slopstation import events
from slopstation.agent.tools.media_clients import (
    MediaConfigurationError,
    MediaError,
    _clean_text,
    _parse_time,
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
# Zero DHT nodes this long, with downloads waiting, before each heal step.
PEERS_DEAD_S = 300
QBIT_RESTART_WAIT_S = 60


def _launch_detached(exe):
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return subprocess.Popen(
        [str(exe)], cwd=str(Path(exe).parent), creationflags=flags, close_fds=True
    )


def default_proton_log_path():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise MediaConfigurationError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "Proton" / "Proton VPN" / "Logs" / "client-logs.txt"


def _no_state(state, path):
    return {
        "state": state,
        "status": None,
        "port": None,
        "observed_at": None,
        "age_s": None,
        "path": str(path),
    }


def read_proton_port_state(path=None, now=None):
    """Read the latest state periodically emitted by Proton's Windows client."""
    source = Path(path) if path is not None else default_proton_log_path()
    backup = source.with_name(f"{source.stem}.1{source.suffix}")
    sources = [candidate for candidate in (backup, source) if candidate.is_file()]
    if not sources:
        return _no_state("missing", source)

    latest: dict[str, Any] | None = None
    for candidate in sources:
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            raise MediaError("Proton client log is unreadable") from e
        for match in PROTON_STATUS_RE.finditer(text):
            observed = _parse_time(match.group("timestamp"))
            if observed is None or (latest and observed < latest["observed"]):
                continue
            port_match = PROTON_PORT_RE.search(match.group("detail"))
            latest = {
                "status": match.group("status"),
                "port": int(port_match.group("port")) if port_match else None,
                "observed": observed,
                "path": candidate,
            }
    if latest is None:
        return _no_state("unknown", source)

    current = now or datetime.datetime.now(datetime.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.UTC)
    age_s = (current - latest["observed"]).total_seconds()
    status = latest["status"]
    port = latest["port"]
    if age_s < -5 or age_s > PROTON_LOG_MAX_AGE_S:
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
    """Hold qBittorrent's listening port to Proton's mapping and its peer
    sockets to an adapter that exists. A reconnect that keeps the port
    changes no preference, so nothing reopens the sockets and DHT stays
    empty; rebind on the reconnect, and when DHT stays empty anyway,
    rebind, then restart."""

    def __init__(
        self,
        client,
        log,
        path=None,
        poll_s=30,
        now=None,
        interface=None,
        exe=None,
        launch=_launch_detached,
        sleep=time.sleep,
    ):
        self.client = client
        self.log = log
        self.path = Path(path) if path is not None else default_proton_log_path()
        self.poll_s = poll_s
        self.now = now
        self.interface = interface
        self.exe = exe
        self.launch = launch
        self.sleep = sleep
        self._last_failure = None
        self._last_state = None
        self._reset_watch()

    def reconcile_once(self):
        source = read_proton_port_state(self.path, now=self.now)
        result = {**source, "changed": False}
        if source["state"] == "missing":
            raise MediaError("Proton client log is missing")
        if source["state"] == "unknown":
            raise MediaError("Proton client log format is unrecognized")
        if source["state"] == "stale":
            raise MediaError("Proton port-forwarding state is stale")
        previous_state, self._last_state = self._last_state, source["state"]
        if source["state"] != "active":
            # No tunnel, no peers: nothing to heal until it is back.
            self._reset_watch()
            return result
        updated = self.client.set_listen_port(source["port"])
        result.update(updated)
        if updated["changed"]:
            self.log(
                "proton_port_synced",
                port=updated["listen_port"],
                previous_port=updated["previous_port"],
                source_age_s=source["age_s"],
            )
        if previous_state not in (None, "active"):
            self._rebind("proton_reconnect")
            self._reset_watch()
        result["dht_nodes"] = self._watch_peers()
        return result

    def _reset_watch(self):
        self._dead_since = None
        self._acted_at = None
        self._step = 0

    def _now(self):
        return self.now or datetime.datetime.now(datetime.UTC)

    def _watch_peers(self):
        """Zero DHT nodes with downloads waiting is the dead-socket symptom
        whatever caused it. Each PEERS_DEAD_S it persists: rebind, then
        restart, then say so once and wait."""
        nodes = int(self.client.transfer_info().get("dht_nodes", 0) or 0)
        if nodes or not self.client.preferences().get("dht", True):
            self._recovered(nodes)
            return nodes
        if not self.client.torrents(filter="downloading"):
            self._recovered(nodes)
            return nodes
        now = self._now()
        if self._dead_since is None:
            self._dead_since = now
        waited_s = (now - (self._acted_at or self._dead_since)).total_seconds()
        if waited_s < PEERS_DEAD_S or self._step >= 3:
            return nodes
        self._acted_at = now
        self._step += 1
        try:
            if self._step == 1:
                dead_s = (now - self._dead_since).total_seconds()
                self.log.warn("qbit_peers_lost", dead_s=round(dead_s))
                self._rebind("peers_lost")
            elif self._step == 2:
                self._restart()
            else:
                raise MediaError("DHT is still empty after a restart")
        except MediaError as e:
            step = "rebind" if self._step == 1 else "restart"
            self.log.error("qbit_heal_failed", step=step, err=_clean_text(e))
        return nodes

    def _recovered(self, nodes):
        if self._step:
            after = "rebind" if self._step == 1 else "restart"
            self.log("qbit_peers_recovered", after=after, nodes=nodes)
        self._reset_watch()

    def _rebind(self, reason):
        if not self.interface:
            raise MediaError("media.qbittorrentNetworkInterface is not set")
        bound = self.client.rebind_interface(self.interface)
        self.log(
            "qbit_rebound",
            reason=reason,
            interface=self.interface,
            drifted=bound["drifted"],
        )

    def _restart(self):
        """A clean shutdown through the API, so resume data is saved; then
        relaunch and wait for the API to answer. Verified by the API and
        the new process, never by process name."""
        if not self.exe:
            raise MediaError("media.qbittorrentExe is not set")
        self.client.shutdown()
        self._wait(lambda: not self._answers(), "qBittorrent did not stop")
        process = self.launch(self.exe)
        self._wait(self._answers, "qBittorrent did not come back")
        if process.poll() is not None:
            raise MediaError("the relaunched qBittorrent exited")
        self.log("qbit_restarted", pid=process.pid)

    def _answers(self):
        try:
            self.client.version()
        except MediaError:
            return False
        return True

    def _wait(self, ready, err):
        for _ in range(QBIT_RESTART_WAIT_S):
            if ready():
                return
            self.sleep(1)
        raise MediaError(err)

    def start(self):
        events.Ticker("proton-port-monitor", self.poll_s, self._tick).start()

    def _tick(self):
        try:
            self.reconcile_once()
            self._last_failure = None
        except Exception as e:
            detail = _clean_text(e)
            # A client that stays down is one line, not one line per poll.
            if detail != self._last_failure:
                self.log.error("proton_port_sync_failed", err=detail)
                self._last_failure = detail
