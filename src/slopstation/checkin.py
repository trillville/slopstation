"""Send independent Sentry liveness check-ins for each lane.

The check-in URL is derived from the DSN:

    https://<key>@<host>/<project>
      -> https://<host>/api/<project>/cron/k15-<lane>/<key>/

Failed check-ins do not stop the lane.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlsplit

from slopstation import config, events

INTERVAL_S = 60
TIMEOUT_S = 5

# Sentry upserts the monitor from this on the first check-in. Two consecutive
# misses at a 1-minute interval page ~4 min after a lane dies, which is fast
# enough to matter and survives one network blip.
MONITOR_CONFIG = {
    "schedule": {"type": "interval", "value": 1, "unit": "minute"},
    "checkin_margin": 2,
    "max_runtime": 1,
    "timezone": "UTC",
    "failure_issue_threshold": 2,
    "recovery_threshold": 1,
}

# Renaming a slug orphans its monitor in Sentry and silently stops paging for
# that lane, so this is as frozen as the event vocabulary.
SLUG_PREFIX = "k15-"


def parse_dsn(dsn: object) -> tuple[str, str, str] | None:
    """(host, project, public_key) from a Sentry DSN, or None when it is
    absent, still a template value, or not a DSN at all. The public key is not
    a secret - it ships inside client apps by design - which is why the whole
    DSN lives in config.json rather than secrets.json."""
    if not events.real_key(dsn):
        return None
    try:
        parts = urlsplit(dsn.strip())
    except ValueError:
        return None
    project = parts.path.strip("/")
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or not parts.username
        or not project.isdigit()
    ):
        return None
    return parts.hostname, project, parts.username


def checkin_url(dsn: object, lane: str) -> str | None:
    parsed = parse_dsn(dsn)
    if parsed is None or not lane:
        return None
    host, project, key = parsed
    return f"https://{host}/api/{project}/cron/{SLUG_PREFIX}{lane}/{key}/"


def send(url: str, status: str = "ok") -> bool:
    """One check-in. POST rather than GET so monitor_config rides along and
    the monitor upserts itself. Never raises."""
    body = json.dumps({"monitor_config": MONITOR_CONFIG, "status": status}).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def start(lane: str, cfg: dict | None = None) -> events.Ticker | None:
    """Check in for `lane` every minute for as long as this process runs.
    Returns None when there is nothing to do: no DSN, or a test run, which
    must never touch a live monitor. The first result is logged, then only
    transitions, so a flapping uplink cannot flood the stream."""
    if events.ENV != "prod":
        return None
    if cfg is None:
        try:
            cfg = config.current()
        except Exception:
            return None  # a box with no config.json yet still runs the lane
    url = checkin_url(cfg.get("sentryDsn"), lane)
    if url is None:
        return None

    slug = SLUG_PREFIX + lane
    was = None

    def tick():
        nonlocal was
        ok = send(url)
        if ok is not was:
            # Two literal calls: _events_scan reads event names out of the
            # source, and a name it cannot see cannot be frozen.
            if ok:
                events.emit(lane, "checkin", events.INFO, monitor=slug)
            else:
                events.emit(lane, "checkin_failed", events.WARN, monitor=slug)
            was = ok

    t = events.Ticker(f"checkin-{lane}", INTERVAL_S, tick)
    t.start()
    return t
