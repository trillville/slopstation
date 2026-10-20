"""The heal that restarts Docker's VM when the apps lose the media drive,
and the search for what went missing meanwhile."""

import dataclasses
import subprocess

from helpers import CapturingLog, FakeArr
from slopstation.agent.media import mount
from slopstation.agent.media.clients import MediaError
from slopstation.agent.media.recovery import search_missing

ROOTS = {"Sonarr": "/data/TV", "Radarr": "/data/Movies"}
LOST = [{"path": "/data/TV", "accessible": False}]
BACK = [{"path": "/data/TV", "accessible": True}]


@dataclasses.dataclass
class App(FakeArr):
    """An app in a container: it stops answering while Docker is down."""

    up: bool = True

    def get(self, endpoint, params=None):
        if not self.up:
            raise MediaError(f"{self.name} is unreachable")
        return super().get(endpoint, params)


class Docker:
    """The docker and wsl command lines. Stopping Docker takes the engine and
    the apps down; `compose up` brings the apps back, with the drive in view
    unless `heals` is off. `fail` maps a step to the output it fails with."""

    def __init__(self, apps, fail=None, refuse_breakaway=False, heals=True):
        self.apps = apps
        self.fail = fail or {}
        self.refuse_breakaway = refuse_breakaway
        self.heals = heals
        self.engine = True
        self.ran = []

    def run(self, argv, stdout=None, creationflags=0, **kwargs):
        if self.refuse_breakaway and creationflags & mount.OUTSIDE_LANE:
            raise PermissionError(5, "Access is denied")
        step = " ".join(argv[1:3]) if argv[0] == "docker" else " ".join(argv)
        self.ran.append(step)
        if step in self.fail:
            stdout.write(self.fail[step])
            return subprocess.CompletedProcess(argv, 1)
        if argv[1] in ("version", "info") and not self.engine:
            return subprocess.CompletedProcess(argv, 1)
        if step == "desktop stop":
            self.engine = False
            for app in self.apps:
                app.up = False
        elif step == "desktop start":
            self.engine = True
        elif argv[1] == "compose":
            for app in self.apps:
                app.set(up=True, roots=BACK if self.heals else LOST)
        return subprocess.CompletedProcess(argv, 0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def monitor(tmp_path, docker, root=None):
    media_root = tmp_path / "M"
    (media_root / "TV").mkdir(parents=True, exist_ok=True)
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    (media_dir / ".env").write_text("", encoding="utf-8")
    clock = Clock()
    log = CapturingLog("voice")
    watch = mount.DockerMountMonitor(
        docker.apps,
        log,
        ROOTS,
        root or media_root,
        media_dir,
        run=docker.run,
        now=clock,
        sleep=clock.sleep,
    )
    return watch, log, clock


def heal(watch):
    """The grace poll, then the poll that acts."""
    watch.reconcile_once()
    watch.reconcile_once()


def test_a_lost_drive_restarts_the_vm_and_searches_again(tmp_path):
    """A failed wsl --shutdown, in output no code page decodes, is not where
    the heal stops: Docker is already stopped and must be started again."""
    sonarr = App("Sonarr").set(roots=LOST)
    docker = Docker([sonarr], fail={"wsl --shutdown": b"\x8f\xff\x00b\x00u\x00s\x00y"})
    watch, log, _ = monitor(tmp_path, docker)
    watch.reconcile_once()
    assert not docker.ran, "one poll of grace before acting"
    watch.reconcile_once()
    assert "desktop start" in docker.ran
    assert log.find("media_mount_restored")
    assert sonarr.posts == [
        ("command", {"name": "MissingEpisodeSearch", "monitored": True})
    ]
    assert not watch.marker.exists()


def test_only_a_configured_root_whose_folder_windows_has_counts(tmp_path):
    """An extra root, or a folder gone on Windows too, is not the VM's
    doing, and restarting it would only take the stack down."""
    extra = App("Sonarr").set(roots=[{"path": "/data/Old", "accessible": False}])
    docker = Docker([extra])
    watch = monitor(tmp_path, docker)[0]
    (tmp_path / "M" / "Old").mkdir()
    heal(watch)
    sonarr = App("Sonarr").set(roots=LOST)
    docker2 = Docker([sonarr])
    heal(monitor(tmp_path, docker2, root=tmp_path / "unplugged")[0])
    assert not docker.ran and not docker2.ran


def test_a_heal_that_leaves_docker_down_is_finished_later(tmp_path):
    """Silent apps after a failed heal are still the heal's: not within the
    hour, and not lost when the lane restarts, because the marker outlives
    it."""
    sonarr = App("Sonarr").set(roots=LOST)
    docker = Docker([sonarr], fail={"desktop start": b"engine error"})
    watch, log, _ = monitor(tmp_path, docker)
    heal(watch)
    assert "engine error" in log.find("media_mount_heal_failed")[-1]["err"]
    watch.reconcile_once()
    assert docker.ran.count("wsl --shutdown") == 1, "not again within the hour"
    docker.fail = {}
    restarted, log, _ = monitor(tmp_path, docker)
    heal(restarted)
    assert log.find("media_mount_restored")


def test_a_heal_the_roots_did_not_come_back_from_is_not_repeated(tmp_path):
    """The apps answer and still cannot reach the drive: the restart was not
    the fix. Roots that come back later, slower than the heal waited, are
    still the recovery."""
    sonarr = App("Sonarr").set(roots=LOST)
    docker = Docker([sonarr], heals=False)
    watch, log, clock = monitor(tmp_path, docker)
    heal(watch)
    assert log.find("media_mount_heal_failed")
    clock.t += 2 * mount.RESTART_COOLDOWN_S
    heal(watch)
    assert docker.ran.count("wsl --shutdown") == 1
    sonarr.set(roots=BACK)
    watch.reconcile_once()
    assert log.find("media_mount_restored")
    assert sonarr.posts


def test_a_lane_that_cannot_break_away_stops_nothing(tmp_path):
    """Stopping Docker and then failing to start it outside the lane's job
    would leave the stack down: the probe has to fail first."""
    sonarr = App("Sonarr").set(roots=LOST)
    docker = Docker([sonarr], refuse_breakaway=True)
    watch, log, _ = monitor(tmp_path, docker)
    heal(watch)
    assert not docker.ran
    err = log.find("media_mount_heal_failed")[-1]["err"]
    assert "restart the Slopstation tasks" in err


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
