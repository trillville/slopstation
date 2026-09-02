"""Blind test: Sentry cron check-ins - the config gate, the self-registering
payload, and the two rules that matter (a test run never checks in, and a
lane starts whether or not its telemetry does). Run:
    .venv\\Scripts\\python tests\\test_checkin.py
"""
import json
import time

import _bootstrap  # noqa: F401

import checkin
import cglib
import events

REAL = {"sentry": {"orgId": "4509876",
                   "projectId": "1234567",
                   "publicKey": "a" * 32}}


def main():

    # -- the config gate -------------------------------------------------------
    assert checkin.sentry_config(REAL) == ("4509876", "1234567", "a" * 32)
    assert checkin.sentry_config({}) is None
    assert checkin.sentry_config({"sentry": {}}) is None
    assert checkin.sentry_config(None) is None
    assert checkin.sentry_config({"sentry": {"orgId": "1", "projectId": "2",
                                             "publicKey": "pk-..."}}) is None
    print("  gate: all three ids required, placeholders read as absent")

    # -- slug and url ----------------------------------------------------------
    # The service prefix keeps a second box off the K15's monitors.
    assert checkin.slug("listener") == f"{events.SERVICE}-listener"
    u = checkin.url(REAL, "listener")
    assert u.startswith("https://o4509876.ingest.sentry.io/api/1234567/cron/")
    assert f"/{checkin.slug('listener')}/" in u, u
    assert "a" * 32 in u, "the key rides the path"
    assert f"environment={events.ENV}" in u
    assert checkin.url({}, "listener") is None
    # A region-scoped DSN host must be used verbatim, not rebuilt from orgId.
    regional = {"sentry": dict(REAL["sentry"],
                               ingestHost="o4509876.ingest.us.sentry.io")}
    assert checkin.ingest_host(REAL) == "o4509876.ingest.sentry.io"
    assert checkin.ingest_host(regional) == "o4509876.ingest.us.sentry.io"
    assert checkin.url(regional, "listener").startswith(
        "https://o4509876.ingest.us.sentry.io/api/1234567/cron/")
    assert checkin.ingest_host({}) == ""
    print(f"  url: .../cron/{checkin.slug('listener')}/<key>/ (region honoured)")

    # -- the payload upserts the monitor ---------------------------------------
    b = json.loads(checkin.body(60))
    assert b["status"] == "ok"
    m = b["monitor_config"]
    # Interval must be whole minutes: Sentry has no sub-minute schedule.
    assert m["schedule"] == {"type": "interval", "value": 1, "unit": "minute"}
    assert m["checkin_margin"] == checkin.MARGIN_MIN
    assert m["failure_issue_threshold"] == checkin.FAILURE_THRESHOLD
    # A sub-minute interval must not round to a zero-minute schedule.
    assert json.loads(checkin.body(5))["monitor_config"]["schedule"]["value"] == 1
    print("  payload: status ok + a monitor_config that creates the monitor")

    # -- a test run never checks in --------------------------------------------
    # Same rule as the JSONL: env=test telemetry cannot leave the box.
    log = cglib.CapturingLog("listener")
    assert events.ENV == "test", "the suite must run with argv[0] under tests/"
    assert checkin.start("listener", log, cfg=REAL) is None
    print("  env: a test run refuses to check in")

    # -- the thread, with the network stubbed ----------------------------------
    posts = []
    real_post, real_env = checkin._post, events.ENV
    checkin._post = lambda u, payload: posts.append((u, payload))
    events.ENV = "prod"
    try:
        log = cglib.CapturingLog("listener")
        t = checkin.start("listener", log, cfg=REAL, interval_s=0.02)
        assert t is not None and t.daemon, "must be a daemon thread"
        deadline = time.time() + 3
        while not posts and time.time() < deadline:
            time.sleep(0.01)
        assert posts, "the thread never posted a check-in"
        assert posts[0][0].startswith("https://o4509876."), posts[0][0]
        up = log.find("lane_up")
        assert up, log.events()
        # The url carries the key, so it must never reach a log line.
        assert not any("a" * 32 in str(v) for v in up[0].values()), up[0]
    finally:
        checkin._post, events.ENV = real_post, real_env
    print("  thread: daemon, posts on its interval, never logs the key")

    # -- fail-soft --------------------------------------------------------------
    log = cglib.CapturingLog("listener")
    events.ENV = "prod"
    try:
        # Unconfigured: quiet None, and the lane starts anyway.
        assert checkin.start("listener", log, cfg={}) is None
        assert log.find("lane_disabled"), log.events()
        # A cfg that explodes on read must not reach the caller. Non-empty
        # on purpose: sentry_config does `(cfg or {})`, so an EMPTY subclass
        # is replaced by a plain {} and its get() is never reached - the test
        # would pass while exercising nothing.
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("bad config")
        assert checkin.start("listener", log, cfg=Boom(sentry=1)) is None
        # No logger at all is legal: chord_listener passes one, a bench may not.
        assert checkin.start("listener", None, cfg={}) is None
    finally:
        events.ENV = real_env
    print("  fail-soft: no config, a throwing config and no logger all return None")

    print("OK - checkin: gate, url, payload, env rule, thread, fail-soft")


if __name__ == "__main__":
    main()
