"""Servarr app updates: which apps have one, and moving one app to it.

The linuxserver images turn off each app's own updater, so an update is a
new image: pull it and recreate that one container. The app's config and
library live on the bind mounts and are untouched.
"""

import datetime
import subprocess
import time

from slopstation.agent.tools.media_checks import compose_command
from slopstation.agent.tools.media_clients import MediaError, _clean_text
from slopstation.agent.tools.monitor import ChangeOnly, Monitor

# A pull is a few hundred MB; the restart takes seconds.
COMPOSE_TIMEOUT_S = 600
READY_TIMEOUT_S = 120
READY_POLL_S = 5
# Minor updates apply in this local hour, when nobody is on the couch. The
# hourly poll lands in it once a night.
UPDATE_HOUR = 4
UPDATE_POLL_S = 3600
# The apps that import files; a restart mid-import is what waiting avoids.
IMPORTERS = frozenset(("Radarr", "Sonarr"))
IMPORTING = frozenset(("importing", "importpending"))


def _status(client):
    status = client.get("system/status")
    if not isinstance(status, dict):
        raise MediaError(f"{client.name} returned invalid status")
    return status


def image_version(client):
    """The running image's version, e.g. 6.3.0.10514-ls314. It is also the
    image tag to pin to when rolling back."""
    status = _status(client)
    return _clean_text(status.get("packageVersion") or status.get("version"), 40)


def available_update(client):
    """{installed, latest, released} when the app reports a newer release
    than the one it runs, else None."""
    releases = client.get("update")
    if not isinstance(releases, list):
        raise MediaError(f"{client.name} returned an invalid update list")
    latest = next(
        (row for row in releases if isinstance(row, dict) and row.get("latest")),
        None,
    )
    if latest is None or latest.get("installed"):
        return None
    return {
        "installed": _clean_text(_status(client).get("version"), 40),
        "latest": _clean_text(latest.get("version"), 40),
        "released": _clean_text(latest.get("releaseDate"), 10),
    }


def _compose(run, media_dir, *args):
    try:
        done = run(
            compose_command(media_dir, *args),
            capture_output=True,
            text=True,
            timeout=COMPOSE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaError(f"docker compose {args[0]} failed: {e}") from e
    if done.returncode:
        raise MediaError(_clean_text(done.stderr) or f"docker compose {args[0]} failed")


def update_app(
    client,
    media_dir,
    run=subprocess.run,
    now=time.monotonic,
    sleep=time.sleep,
):
    """Pull the app's image, recreate its container and wait for the app to
    answer again. Returns the image versions before and after; the same
    version twice means linuxserver has not published the release yet."""
    before = image_version(client)
    service = client.name.lower()
    _compose(run, media_dir, "pull", service)
    _compose(run, media_dir, "up", "-d", service)
    deadline = now() + READY_TIMEOUT_S
    while True:
        try:
            after = image_version(client)
            break
        except MediaError:
            if now() >= deadline:
                raise MediaError(
                    f"{client.name} did not answer within {READY_TIMEOUT_S} s "
                    "of the restart"
                ) from None
            sleep(READY_POLL_S)
    return {"app": client.name, "before": before, "after": after}


class MediaUpdateMonitor(Monitor):
    """Apply minor app updates overnight and log each one. A new major version
    is held for a person: its database migration may not survive rolling the
    image back."""

    THREAD_NAME = "media-update-monitor"

    def __init__(
        self,
        clients,
        log,
        media_dir,
        poll_s=UPDATE_POLL_S,
        update=update_app,
        clock=datetime.datetime.now,
    ):
        self.clients = tuple(clients)
        self.log = log
        self.media_dir = media_dir
        self.poll_s = poll_s
        self.update = update
        self.clock = clock
        self._held = ChangeOnly()

    def reconcile_once(self):
        if self.clock().hour != UPDATE_HOUR:
            return
        for client in self.clients:
            try:
                self._update(client)
            except MediaError as e:
                self.log.error(
                    "media_update_failed", app=client.name, err=_clean_text(e)
                )

    def _update(self, client):
        found = available_update(client)
        if found is None:
            return
        if found["latest"].split(".")[0] != found["installed"].split(".")[0]:
            if self._held.changed(client.name, found["latest"]):
                self.log.warn(
                    "media_update_held",
                    app=client.name,
                    installed=found["installed"],
                    latest=found["latest"],
                )
            return
        if client.name in IMPORTERS and self._importing(client):
            return
        result = self.update(client, self.media_dir)
        # The same image twice: linuxserver has not built the release yet,
        # and tomorrow night tries again.
        if result["before"] != result["after"]:
            self.log(
                "media_update_applied",
                app=client.name,
                before=result["before"],
                after=result["after"],
            )

    def _importing(self, client):
        page = client.get("queue", {"pageSize": 50})
        records = page.get("records") if isinstance(page, dict) else None
        return any(
            isinstance(row, dict)
            and _clean_text(row.get("trackedDownloadState"), 30).lower() in IMPORTING
            for row in records or ()
        )
