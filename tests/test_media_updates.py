"""Servarr app updates: finding a newer release, pulling it into one app's
container, and the overnight monitor that applies minor ones."""

import datetime
import subprocess

import pytest

from helpers import CapturingLog
from slopstation.agent.media import clients, updates


class FakeServarr:
    """One app's status and release list. Each status read takes the next
    entry of `statuses`; None is the app not answering, as mid-restart."""

    name = "Radarr"

    def __init__(self, statuses, releases=()):
        self.statuses = list(statuses)
        self.releases = list(releases)

    def get(self, endpoint, params=None):
        if endpoint == "update":
            return list(self.releases)
        if endpoint == "queue":
            return self.queue
        assert endpoint == "system/status"
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise clients.MediaError("media service is unreachable")
        return status


RADARR_6_3 = {"version": "6.3.0.10514", "packageVersion": "6.3.0.10514-ls314"}
RADARR_6_4 = {"version": "6.4.4.10685", "packageVersion": "6.4.4.10685-ls320"}


def test_available_update_names_a_newer_release_only():
    releases = [
        {
            "version": "6.4.4.10685",
            "latest": True,
            "installed": False,
            "releaseDate": "2026-09-16T18:13:40Z",
        },
        {"version": "6.3.0.10514", "latest": False, "installed": True},
    ]
    assert updates.available_update(FakeServarr([RADARR_6_3], releases)) == {
        "installed": "6.3.0.10514",
        "latest": "6.4.4.10685",
        "released": "2026-09-16",
    }
    current = [dict(releases[0], installed=True)]
    assert updates.available_update(FakeServarr([RADARR_6_4], current)) is None


class FakeDocker:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command[command.index("--env-file") + 2 :])
        return subprocess.CompletedProcess(command, self.returncode, "", "denied")


def _media_dir(tmp_path):
    (tmp_path / ".env").write_text("MEDIA_ROOT=media", encoding="utf-8")
    return tmp_path


def test_update_recreates_one_container_and_waits_for_the_app(tmp_path):
    docker = FakeDocker()
    app = FakeServarr([RADARR_6_3, None, None, RADARR_6_4])
    result = updates.update_app(
        app, _media_dir(tmp_path), run=docker, now=lambda: 0, sleep=lambda s: None
    )
    assert docker.commands == [["pull", "radarr"], ["up", "-d", "radarr"]]
    assert result == {
        "app": "Radarr",
        "before": "6.3.0.10514-ls314",
        "after": "6.4.4.10685-ls320",
    }


def test_update_fails_on_a_refused_pull_or_an_app_that_never_returns(tmp_path):
    with pytest.raises(clients.MediaError, match="denied"):
        updates.update_app(
            FakeServarr([RADARR_6_3]), _media_dir(tmp_path), run=FakeDocker(1)
        )
    clock = iter(range(0, 1000, 60))
    with pytest.raises(clients.MediaError, match="did not answer"):
        updates.update_app(
            FakeServarr([RADARR_6_3, None]),
            _media_dir(tmp_path),
            run=FakeDocker(),
            now=lambda: next(clock),
            sleep=lambda s: None,
        )


def _night_watch(apps, update, hour=4):
    log = CapturingLog()
    watch = updates.MediaUpdateMonitor(
        apps,
        log,
        "media",
        update=update,
        clock=lambda: datetime.datetime(2026, 9, 27, hour, 5),
    )
    watch.reconcile_once()
    return log


def _offered(app, installed, latest, queue=()):
    server = FakeServarr(
        [{"version": installed}], [{"version": latest, "latest": True}]
    )
    server.name = app
    server.queue = {"records": list(queue)}
    return server


def test_update_watch_applies_minors_overnight_and_holds_majors():
    updated = []

    def update(client, media_dir):
        updated.append(client.name)
        return {"app": client.name, "before": "6.3.0-ls314", "after": "6.4.4-ls320"}

    radarr = _offered("Radarr", "6.3.0", "6.4.4")
    sonarr = _offered("Sonarr", "4.0.19", "5.0.0")
    assert _night_watch([radarr, sonarr], update, hour=15).records == []
    log = _night_watch([radarr, sonarr], update)
    assert updated == ["Radarr"]
    assert log.find("media_update_applied")[0]["after"] == "6.4.4-ls320"
    assert log.find("media_update_held")[0]["latest"] == "5.0.0"


def test_update_watch_waits_out_an_import_and_tells_a_failed_update_from_a_skip():
    def update(client, media_dir):
        raise clients.MediaError("pull access denied")

    importing = _offered(
        "Radarr", "6.3.0", "6.4.4", [{"trackedDownloadState": "importing"}]
    )
    down = FakeServarr([None], [{"version": "4.0.20", "latest": True}])
    down.name = "Sonarr"
    prowlarr = _offered("Prowlarr", "2.5.2", "2.6.5")
    log = _night_watch([importing, down, prowlarr], update)
    assert [(r["event"], r["app"]) for r in log.records] == [
        ("media_update_skipped", "Sonarr"),
        ("media_update_failed", "Prowlarr"),
    ]
