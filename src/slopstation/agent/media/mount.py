"""Restart Docker Desktop's VM when Radarr or Sonarr loses a media folder
that Windows still has. Why that happens, and the steps by hand: 'Media
drive lost' in media/README.md."""

import subprocess
import tempfile
import time
from pathlib import Path

from slopstation import paths
from slopstation.agent.media.clients import MediaError, clean_text
from slopstation.agent.media.doctor import compose_command
from slopstation.agent.media.processes import NO_WINDOW, OUTSIDE_LANE
from slopstation.agent.media.recovery import search_missing
from slopstation.agent.media.storage import host_path
from slopstation.agent.monitor import Monitor

MOUNT_POLL_S = 300
RESTART_COOLDOWN_S = 3600
STEP_TIMEOUT_S = 120
READY_TIMEOUT_S = 300
READY_POLL_S = 10


class DockerMountMonitor(Monitor):
    """`roots` names the root folder each app is configured with. Only those
    count: another root an app cannot reach is not the VM's doing.

    A marker file stands from just before Docker is stopped until the roots
    are back, so a lane restarted mid-heal, or after a heal that failed,
    still knows the stack may be down because of it."""

    THREAD_NAME = "docker-mount-monitor"

    def __init__(
        self,
        clients,
        log,
        roots,
        media_root,
        media_dir,
        poll_s=MOUNT_POLL_S,
        run=subprocess.run,
        now=time.monotonic,
        sleep=time.sleep,
    ):
        self.clients = tuple(clients)
        self.log = log
        self.roots = {name: str(path).rstrip("/") for name, path in roots.items()}
        self.media_root = Path(media_root)
        self.media_dir = media_dir
        self.poll_s = poll_s
        self.run = run
        self.now = now
        self.sleep = sleep
        self.marker = paths.state("docker-mount-heal")
        self._lost_since = None
        self._restarted_at = None

    def reconcile_once(self):
        lost, silent = self._state()
        healing = self.marker.exists()
        if not lost and not silent:
            if healing:
                # Back after a heal that was reported failed: the apps were
                # slow to start, or a restarted lane missed the ending.
                self._recovered()
            self._lost_since = None
            return
        if healing:
            if lost:
                # The apps answer and still cannot reach the drive: the
                # restart did not fix it, so another will not either.
                return
            # Silent apps after a heal that stopped Docker are this heal's.
            lost = [f"{name} not answering" for name in silent]
        elif not lost:
            return
        now = self.now()
        if self._lost_since is None:
            # One poll of grace: an app still starting can call its root
            # missing.
            self._lost_since = now
            return
        if (
            self._restarted_at is not None
            and now - self._restarted_at < RESTART_COOLDOWN_S
        ):
            return
        self._restarted_at = now
        self.log.warn(
            "media_mount_lost",
            roots="; ".join(lost),
            lost_s=round(now - self._lost_since),
        )
        try:
            self._restart_vm()
        except MediaError as e:
            self.log.error("media_mount_heal_failed", err=clean_text(e))
            return
        self._recovered(waited_s=round(self.now() - now))

    def _state(self):
        """(lost, silent): "App path" for each configured root its app cannot
        reach while Windows has the folder, and the apps that did not
        answer."""
        lost, silent = [], []
        for client in self.clients:
            try:
                rows = client.get("rootfolder")
            except MediaError:
                silent.append(client.name)
                continue
            for row in rows if isinstance(rows, list) else ():
                if not isinstance(row, dict) or row.get("accessible") is not False:
                    continue
                path = str(row.get("path", "")).rstrip("/")
                if path != self.roots.get(client.name):
                    continue
                # Gone on Windows too: a deleted folder or an unplugged
                # drive, which no VM restart brings back.
                folder = host_path(self.media_root, path)
                if folder is not None and folder.is_dir():
                    lost.append(f"{client.name} {path}")
        return lost, silent

    def _recovered(self, waited_s=None):
        self.marker.unlink(missing_ok=True)
        self._lost_since = None
        self.log("media_mount_restored", waited_s=waited_s)
        search_missing(self.clients, self.log, "docker_mount")

    def _restart_vm(self):
        """Docker Desktop is started outside the lane's job, so the next lane
        restart does not end it. The probe needs only the client, and runs
        before anything is stopped: a lane whose job refuses breakaway must
        fail with Docker still up."""
        self._call("breakaway probe", ["docker", "--version"], OUTSIDE_LANE)
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.touch()
        self._call("docker desktop stop", ["docker", "desktop", "stop"])
        shutdown_err = ""
        try:
            self._call("wsl --shutdown", ["wsl", "--shutdown"])
        except MediaError as e:
            # Docker is stopped by now: start it again either way.
            shutdown_err = f" ({e})"
        self._call(
            "docker desktop start",
            ["docker", "desktop", "start", "--detach"],
            OUTSIDE_LANE,
        )
        self._wait(self._engine_up, "the Docker engine did not answer")
        self._call("docker compose up", compose_command(self.media_dir, "up", "-d"))
        self._wait(
            self._roots_back, "the apps still cannot reach their roots" + shutdown_err
        )

    def _engine_up(self):
        try:
            self._call("docker info", ["docker", "info"])
        except MediaError:
            return False
        return True

    def _roots_back(self):
        lost, silent = self._state()
        return not lost and not silent

    def _call(self, what, argv, flags=0):
        """Output goes to a file, not a pipe: Docker Desktop or a compose
        plugin can inherit a pipe and hold the call open past its timeout."""
        with tempfile.TemporaryFile() as out:
            try:
                done = self.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=STEP_TIMEOUT_S,
                    check=False,
                    creationflags=NO_WINDOW | flags,
                )
            except PermissionError as e:
                raise MediaError(
                    f"{what}: the lane may not start a process outside its job; "
                    "restart the Slopstation tasks"
                ) from e
            except (OSError, subprocess.SubprocessError) as e:
                raise MediaError(f"{what} failed: {e}") from e
            if done.returncode:
                out.seek(0)
                # wsl writes UTF-16 and docker UTF-8: drop the NULs and decode
                # leniently, so neither can stop the heal.
                raw = out.read().replace(b"\x00", b"")
                detail = clean_text(raw.decode("utf-8", "replace"))
                raise MediaError(
                    f"{what} failed: {detail or f'exit {done.returncode}'}"
                )

    def _wait(self, ready, err):
        deadline = self.now() + READY_TIMEOUT_S
        while not ready():
            if self.now() >= deadline:
                raise MediaError(f"{err} within {READY_TIMEOUT_S} s")
            self.sleep(READY_POLL_S)
