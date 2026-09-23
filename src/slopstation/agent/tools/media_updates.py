"""Servarr app updates: which apps have one, and moving one app to it.

The linuxserver images turn off each app's own updater, so an update is a
new image: pull it and recreate that one container. The app's config and
library live on the bind mounts and are untouched.
"""

import subprocess
import time

from slopstation.agent.tools.media_checks import compose_command
from slopstation.agent.tools.media_clients import MediaError, _clean_text

# A pull is a few hundred MB; the restart takes seconds.
COMPOSE_TIMEOUT_S = 600
READY_TIMEOUT_S = 120
READY_POLL_S = 5


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
