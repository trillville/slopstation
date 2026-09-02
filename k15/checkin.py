"""Sentry cron check-ins: a lane proving it is alive, off the log path.

The ABSENCE of a check-in is the alert - Sentry opens the issue itself - so
nothing here reports a failed POST. That independence is the point: the log
shipper dying can no longer blind lane liveness, and the two failures now
have separate detectors.

Stdlib only and never blocking: the chord lane runs on system python, and
events.py's rule (no dependency, no socket, no block) applies to the emit
path, not here - this module IS the socket, so it owns a timeout, a daemon
thread, and a bare except around every request.

The monitor config rides every check-in, so a monitor creates and updates
itself; nothing is clicked in the UI. Test runs never check in: env=test
JSONL cannot leave the box either.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from typing import Any

import events

INTERVAL_S = 60

# Minutes of grace past a missed tick. 2 over a 60 s interval absorbs one
# lost request without paging; Sentry's own minimum is 1.
MARGIN_MIN = 2

# Consecutive misses before Sentry opens the issue. 2 means a page lands
# ~3-4 min after a lane dies - the drill in events.start_heartbeat measures
# ~7-10 min for the Grafana rule this replaces.
FAILURE_THRESHOLD = 2

# Well under INTERVAL_S: a hung uplink must not stack ticks.
TIMEOUT_S = 10


def sentry_config(cfg: dict) -> tuple[str, str, str] | None:
    """(org, project, publicKey), or None when Sentry is not configured.

    The one reader of the block: tracing.py imports this rather than parsing
    it again, so the two lanes cannot drift on what "configured" means.

    Absence is normal and silent - a box without the block simply does not
    check in, which is what lets the deploy land before either machine has
    been touched. The key is public by design (it ships inside client apps);
    it is in config.json, not secrets.json, because it rides the URL."""
    s = (cfg or {}).get("sentry") or {}
    org = str(s.get("orgId") or "").strip()
    project = str(s.get("projectId") or "").strip()
    key = str(s.get("publicKey") or "").strip()
    if not (org and project and events.real_key(key)):
        return None
    return org, project, key


def ingest_host(cfg: dict) -> str:
    """The DSN's own hostname. Newer orgs get a region segment
    (o123.ingest.us.sentry.io) and older ones do not, and only the DSN says
    which - so it is configurable, defaulting to the regionless form. Copy it
    verbatim out of the DSN rather than reasoning about it."""
    parts = sentry_config(cfg)
    host = str(((cfg or {}).get("sentry") or {}).get("ingestHost") or "").strip()
    return host.rstrip("/") or (f"o{parts[0]}.ingest.sentry.io" if parts else "")


def slug(lane: str) -> str:
    """Monitor slug. Carries the service so a second box cannot collide with
    the K15's monitors."""
    return f"{events.SERVICE}-{lane}"


def url(cfg: dict, lane: str) -> str | None:
    parts = sentry_config(cfg)
    if not parts:
        return None
    _, project, key = parts
    return (f"https://{ingest_host(cfg)}/api/{project}"
            f"/cron/{slug(lane)}/{key}/?environment={events.ENV}")


def body(interval_s: float = INTERVAL_S) -> bytes:
    """An OK check-in that also upserts the monitor. POST, not GET: the
    config cannot ride query params."""
    return json.dumps({
        "status": "ok",
        "monitor_config": {
            "schedule": {"type": "interval",
                         "value": max(1, int(interval_s // 60)),
                         "unit": "minute"},
            "checkin_margin": MARGIN_MIN,
            "failure_issue_threshold": FAILURE_THRESHOLD,
            "timezone": "UTC",
        },
    }).encode("utf-8")


def _post(u: str, payload: bytes) -> None:
    """One check-in. Swallows everything - a check-in that cannot report its
    own failure is the design, not an oversight."""
    try:
        req = urllib.request.Request(
            u, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=TIMEOUT_S).close()
    except Exception:
        pass


def start(lane: str, log: Any = None, cfg: dict | None = None,
          interval_s: float = INTERVAL_S) -> threading.Thread | None:
    """Check in for `lane` every interval_s from a daemon thread.

    Returns the thread, or None when this is a test run, when Sentry is not
    configured, or when anything at all goes wrong. Callers do not check the
    result: a lane must start whether or not its telemetry does."""
    try:
        if events.ENV != "prod":
            return None
        if cfg is None:
            import cglib
            cfg = cglib.config()
        u = url(cfg, lane)
        if not u:
            if log:
                log("lane_disabled", what="checkin",
                    reason="no sentry block in config.json")
            return None
        payload = body(interval_s)

        def tick() -> None:
            while True:
                _post(u, payload)
                time.sleep(interval_s)

        t = threading.Thread(target=tick, daemon=True, name=f"checkin-{lane}")
        t.start()
        if log:
            # Never the url: it carries the key, and scrub() cannot see a
            # secret it was never given.
            log("lane_up", what="checkin", poll_s=interval_s)
        return t
    except Exception as e:
        if log:
            try:
                log.warn("lane_disabled", what="checkin", err=repr(e))
            except Exception:
                pass
        return None
