"""The heals that bring the media stack back after the Docker VM loses the
media drive, and the search for what went missing meanwhile."""

import subprocess

from helpers import CapturingLog, FakeArr
from slopstation.agent.media import mount
from slopstation.agent.media.clients import MediaError
from slopstation.agent.media.recovery import search_missing

LOST = [{"path": "/data/TV", "accessible": False}]
BACK = [{"path": "/data/TV", "accessible": True}]


class Docker:
    """The docker and wsl command lines: every call recorded, and `compose
    up` puts the drive back in the apps' view unless told not to."""

    def __init__(self, arrs, heals=True, refuse_breakaway=False):
        self.arrs = arrs
        self.heals = heals
        self.refuse_breakaway = refuse_breakaway
        self.calls = []

    def run(self, argv, creationflags=0, **kwargs):
        if self.refuse_breakaway and creationflags & mount.OUTSIDE_LANE:
            raise PermissionError(5, "Access is denied")
        self.calls.append(" ".join(argv[:3]))
        if argv[:2] == ["docker", "compose"] and "up" in argv and self.heals:
            for arr in self.arrs:
                arr.set(roots=BACK)
        return subprocess.CompletedProcess(argv, 0, "", "")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def monitor(tmp_path, docker, arrs, searched=None):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / ".env").write_text("MEDIA_ROOT=x\n", encoding="utf-8")
    clock = Clock()
    log = CapturingLog("voice")
    watch = mount.DockerMountMonitor(
        arrs,
        log,
        tmp_path,
        media_dir,
        run=docker.run,
        now=clock,
        sleep=clock.sleep,
        on_recovered=None if searched is None else searched.append,
    )
    return watch, log, clock


def test_a_lost_drive_restarts_the_vm_and_searches_again(tmp_path):
    sonarr = FakeArr("Sonarr").set(roots=LOST)
    radarr = FakeArr("Radarr").set(roots=BACK)
    docker = Docker([sonarr, radarr])
    searched = []
    watch, log, _ = monitor(tmp_path, docker, [sonarr, radarr], searched)

    watch.reconcile_once()
    assert not docker.calls, "one poll of grace before acting"
    watch.reconcile_once()
    assert docker.calls == [
        "docker version",
        "docker desktop stop",
        "wsl --shutdown",
        "docker desktop start",
        "docker info --format",
        "docker compose --project-directory",
    ]
    assert log.find("media_mount_lost")[-1]["roots"] == "Sonarr /data/TV"
    assert log.find("media_mount_restored")
    assert searched == ["docker_mount"]


def test_a_drive_windows_cannot_see_either_is_left_alone(tmp_path):
    sonarr = FakeArr("Sonarr").set(roots=LOST)
    docker = Docker([sonarr])
    watch, log, _ = monitor(tmp_path, docker, [sonarr])
    watch.media_root = tmp_path / "unplugged"
    for _ in range(3):
        watch.reconcile_once()
    assert not docker.calls
    assert not log.find("media_mount_lost")


def test_a_failed_restart_is_reported_and_retried_an_hour_later(tmp_path):
    sonarr = FakeArr("Sonarr").set(roots=LOST)
    docker = Docker([sonarr], heals=False)
    watch, log, clock = monitor(tmp_path, docker, [sonarr])
    watch.reconcile_once()
    watch.reconcile_once()
    assert "within" in log.find("media_mount_heal_failed")[-1]["err"]
    attempts = docker.calls.count("wsl --shutdown")
    watch.reconcile_once()
    assert docker.calls.count("wsl --shutdown") == attempts
    clock.t += mount.RESTART_COOLDOWN_S
    watch.reconcile_once()
    assert docker.calls.count("wsl --shutdown") == attempts + 1


def test_a_lane_that_cannot_break_away_stops_nothing(tmp_path):
    """Stopping Docker and then failing to start it outside the lane's job
    would leave the stack down: the probe has to fail first."""
    sonarr = FakeArr("Sonarr").set(roots=LOST)
    docker = Docker([sonarr], refuse_breakaway=True)
    watch, log, _ = monitor(tmp_path, docker, [sonarr])
    watch.reconcile_once()
    watch.reconcile_once()
    assert not docker.calls
    assert (
        "restart the Slopstation tasks"
        in log.find("media_mount_heal_failed")[-1]["err"]
    )


def test_search_missing_asks_each_app_and_reports_one_that_refuses():
    class Down(FakeArr):
        def post(self, endpoint, payload):
            raise MediaError("Radarr is unreachable")

    sonarr = FakeArr("Sonarr")
    log = CapturingLog("voice")
    search_missing([Down("Radarr"), sonarr], log, "qbit_rebind")
    assert sonarr.posts == [
        ("command", {"name": "MissingEpisodeSearch", "monitored": True})
    ]
    assert log.find("media_missing_searched")[-1]["app"] == "Sonarr"
    failed = log.find("media_missing_search_failed")[-1]
    assert (failed["app"], failed["after"]) == ("Radarr", "qbit_rebind")
