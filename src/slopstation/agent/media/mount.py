"""Docker Desktop's VM maps Windows drives only when it starts. A media drive
that arrives later - a reboot that brings Docker up first, or a replug -
leaves /data a 128 MB placeholder on the VM's own disk: Radarr and Sonarr
report their root folders missing and reject every release for lack of
space. No container restart fixes that; restarting the VM does."""

import subprocess
import time
from pathlib import Path

from slopstation.agent.media.clients import MediaError, clean_text
from slopstation.agent.media.doctor import compose_command
from slopstation.agent.media.proton import OUTSIDE_LANE
from slopstation.agent.monitor import Monitor

MOUNT_POLL_S = 300
# One restart an hour at most: a restart that did not take is reported, not
# repeated in a loop that keeps the apps down.
RESTART_COOLDOWN_S = 3600
STEP_TIMEOUT_S = 120
READY_TIMEOUT_S = 300
READY_POLL_S = 10
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def root_state(clients):
    """(lost, silent): "App path" for each root folder an app cannot reach,
    and the apps that did not answer at all."""
    lost, silent = [], []
    for client in clients:
        try:
            rows = client.get("rootfolder")
        except MediaError:
            silent.append(client.name)
            continue
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and row.get("accessible") is False:
                lost.append(f"{client.name} {clean_text(row.get('path'), 80)}")
    return lost, silent


class DockerMountMonitor(Monitor):
    """Restart Docker Desktop's VM when the apps lose a media drive that
    Windows still has, then bring the stack back up."""

    THREAD_NAME = "docker-mount-monitor"

    def __init__(
        self,
        clients,
        log,
        media_root,
        media_dir,
        poll_s=MOUNT_POLL_S,
        run=subprocess.run,
        now=time.monotonic,
        sleep=time.sleep,
        on_recovered=None,
    ):
        self.clients = tuple(clients)
        self.log = log
        self.media_root = Path(media_root)
        self.media_dir = Path(media_dir)
        self.poll_s = poll_s
        self.run = run
        self.now = now
        self.sleep = sleep
        self.on_recovered = on_recovered
        self._lost_since = None
        self._restarted_at = None
        self._failed = False

    def reconcile_once(self):
        lost, silent = root_state(self.clients)
        if self._failed:
            # A restart that failed partway can leave Docker stopped, and
            # then the apps do not answer at all. That is still this heal's
            # to retry, not a recovery.
            lost += [f"{name} not answering" for name in silent]
        if not lost:
            self._lost_since = None
            self._failed = False
            return
        # The drive itself is gone: disk_watch reports that, and no VM
        # restart brings it back.
        if not self.media_root.is_dir():
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
            self._failed = True
            self.log.error("media_mount_heal_failed", err=clean_text(e))
            return
        self._failed = False
        self._lost_since = None
        self.log("media_mount_restored", waited_s=round(self.now() - now))
        if self.on_recovered is not None:
            self.on_recovered("docker_mount")

    def _restart_vm(self):
        """Docker Desktop is started outside the lane's job, so the next lane
        restart does not end it. The probe comes first: a lane whose job
        refuses that must fail before it stops anything."""
        self._call("breakaway probe", ["docker", "version"], OUTSIDE_LANE)
        self._call("docker desktop stop", ["docker", "desktop", "stop"])
        self._call("wsl --shutdown", ["wsl", "--shutdown"])
        # Detached, and no pipes: Docker Desktop could inherit them and hold
        # the call open for as long as it runs.
        self._call(
            "docker desktop start",
            ["docker", "desktop", "start", "--detach"],
            OUTSIDE_LANE,
            quiet=True,
        )
        self._wait(
            lambda: self._ok(["docker", "info", "--format", "{{.ServerVersion}}"]),
            "the Docker engine did not answer",
        )
        self._call("docker compose up", compose_command(self.media_dir, "up", "-d"))
        self._wait(self._roots_back, "the apps still cannot reach their roots")

    def _roots_back(self):
        lost, silent = root_state(self.clients)
        return not lost and not silent

    def _call(self, what, argv, flags=0, quiet=False):
        out = subprocess.DEVNULL if quiet else subprocess.PIPE
        try:
            done = self.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                text=True,
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
            detail = "" if quiet else clean_text(done.stderr or done.stdout)
            raise MediaError(f"{what} failed: {detail or f'exit {done.returncode}'}")

    def _ok(self, argv):
        try:
            self._call(argv[0], argv)
        except MediaError:
            return False
        return True

    def _wait(self, ready, err):
        deadline = self.now() + READY_TIMEOUT_S
        while not ready():
            if self.now() >= deadline:
                raise MediaError(f"{err} within {READY_TIMEOUT_S} s")
            self.sleep(READY_POLL_S)
