"""Sentry cron check-ins: one monitor per lane, so a dead lane pages by itself.

Liveness is the one signal that must not ride the log shipper - a dead
collector and a dead lane look identical in the log stream, and a missed
check-in is an alert by construction rather than a threshold that has to
evaluate an empty window. Every other alert can be a log query; this cannot.

Everything the check-in URL needs is inside the DSN, so this adds no config
key of its own:

    https://<key>@<host>/<project>
      -> https://<host>/api/<project>/cron/k15-<lane>/<key>/

Stdlib only, like every module in this directory (events.py documents why),
and fail-soft throughout: a check-in that cannot be sent costs telemetry,
never the lane.

BILLING: every Sentry plan includes ONE cron monitor and the second is a PAYG
line item. With no budget set, the second lane's check-in is rejected and its
monitor never registers - which reads exactly like a lane that never started.
doctor.py's check-in row is what tells those apart.

DRILL: kill a lane and confirm its monitor - and only its monitor - pages.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from urllib.parse import urlsplit

import events

INTERVAL_S = 60
TIMEOUT_S = 5

# Sentry upserts the monitor from this on the first check-in, so a rebuilt org
# needs no clicking. checkin_margin is how many minutes late a check-in may
# be; two consecutive misses at a 1-minute interval page ~4 min after a lane
# dies, which is fast enough to matter and survives one network blip.
MONITOR_CONFIG = {
    "schedule": {"type": "interval", "value": 1, "unit": "minute"},
    "checkin_margin": 2,
    "max_runtime": 1,
    "timezone": "UTC",
    "failure_issue_threshold": 2,
    "recovery_threshold": 1,
}

# Renaming a slug orphans its monitor in Sentry and silently stops paging for
# that lane, so these are as frozen as the event vocabulary.
SLUG_PREFIX = "k15-"

# Set by start(); read by doctor.py to tell "never configured" from "cannot
# reach Sentry". None until a lane has tried at least once.
last_ok: bool | None = None


def parse_dsn(dsn: object) -> tuple[str, str, str] | None:
    """(host, project, public_key) from a Sentry DSN, or None when it is
    absent, still a template value, or not a DSN at all. The public key is not
    a secret - it ships inside client apps by design - which is why the whole
    DSN lives in config.json rather than secrets.json."""
    if not events.real_key(dsn):
        return None
    try:
        parts = urlsplit(dsn.strip())            # type: ignore[union-attr] # real_key proved str
    except ValueError:
        return None
    project = parts.path.strip("/")
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or not parts.username or not project.isdigit()):
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
    body = json.dumps({"monitor_config": MONITOR_CONFIG,
                       "status": status}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _config_dsn() -> object:
    """config.json's sentryDsn, read at CALL time and never at import: the
    chord lane must import on a machine that has no config.json yet."""
    try:
        import cglib
        return cglib.config().get("sentryDsn")
    except Exception:
        return None


def start(lane: str, cfg: dict | None = None) -> threading.Thread | None:
    """Check in for `lane` every minute for as long as this process runs.
    Returns None when there is nothing to do - no DSN, or a test run. A test
    must never touch a live monitor, the same rule as env=test JSONL.

    The first result is logged once per process (a lane that cannot reach
    Sentry is worth one line, not one a minute); after that only transitions
    are, so a flapping uplink cannot flood the stream.
    """
    if events.ENV != "prod":
        return None
    dsn = cfg.get("sentryDsn") if cfg is not None else _config_dsn()
    url = checkin_url(dsn, lane)
    if url is None:
        return None

    slug = SLUG_PREFIX + lane

    def tick():
        global last_ok
        was = None
        while True:
            try:
                ok = send(url)
                last_ok = ok
                if ok is not was:
                    # Two literal calls, not one with a conditional name:
                    # _events_scan reads event names out of the SOURCE, and a
                    # name it cannot see is a name test_event_names cannot
                    # freeze.
                    if ok:
                        events.emit(lane, "checkin", events.INFO, monitor=slug)
                    else:
                        events.emit(lane, "checkin_failed", events.WARN,
                                    monitor=slug)
                    was = ok
            except Exception:
                pass
            time.sleep(INTERVAL_S)

    t = threading.Thread(target=tick, daemon=True, name=f"checkin-{lane}")
    t.start()
    return t
