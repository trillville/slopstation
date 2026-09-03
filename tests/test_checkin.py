"""Cron check-ins - the URL built out of the DSN, the upsert
payload, and the fail-soft rules that keep a lane alive when Sentry is not.
"""

import json
import time

import pytest

from slopstation import checkin, events, paths

DSN = "https://abc123def456abc123def456abc12345@o4509876.ingest.us.sentry.io/1234567"
URL = (
    "https://o4509876.ingest.us.sentry.io/api/1234567/cron/"
    "k15-listener/abc123def456abc123def456abc12345/"
)


# -- parsing the DSN -----------------------------------------------------------


def test_parse_dsn_splits_host_project_and_key():
    host, project, key = checkin.parse_dsn(DSN)
    assert host == "o4509876.ingest.us.sentry.io", host
    assert project == "1234567", project
    assert key == "abc123def456abc123def456abc12345", key
    # An older org's DSN carries no region; the host is used verbatim either way.
    assert (
        checkin.parse_dsn("https://k0000000000000000000@o1.ingest.sentry.io/42")[0]
        == "o1.ingest.sentry.io"
    )


# Absent, template junk, and things that are not DSNs all read as absent -
# the same gate every other keyed lane uses.
@pytest.mark.parametrize(
    "junk",
    [
        None,
        "",
        "https://...@o1.ingest.sentry.io/1",
        12345,
        "PLACEHOLDER_PUT_YOUR_DSN_HERE_PLEASE",
        "https://o4509876.ingest.us.sentry.io/1234567",  # no key
        "https://k0000000000000000000@o1.ingest.sentry.io/",  # no project
        "https://k0000000000000000000@o1.ingest.sentry.io/abc",  # not a number
        "ftp://k0000000000000000000@o1.ingest.sentry.io/1",
    ],
)
def test_junk_dsn_reads_as_absent(junk):
    assert checkin.parse_dsn(junk) is None, junk
    assert checkin.checkin_url(junk, "listener") is None, junk


# -- the check-in URL ----------------------------------------------------------


def test_checkin_url_is_built_out_of_the_dsn():
    url = checkin.checkin_url(DSN, "listener")
    assert url == URL, url
    # One monitor PER LANE: the slugs must differ, or one lane's check-in
    # silences the other's alert.
    assert checkin.checkin_url(DSN, "voice") != url
    assert checkin.checkin_url(DSN, "") is None


# -- the upsert payload --------------------------------------------------------


def test_monitor_config_is_a_json_upsert_naming_a_schedule():
    # Sentry registers the monitor from this, so a rebuilt org needs no
    # clicking. It has to survive json.dumps and name a schedule.
    body = json.loads(
        json.dumps({"monitor_config": checkin.MONITOR_CONFIG, "status": "ok"})
    )
    assert body["monitor_config"]["schedule"]["type"] == "interval"
    assert body["monitor_config"]["schedule"]["unit"] == "minute"
    # Two misses before an issue opens, so one network blip does not page.
    assert checkin.MONITOR_CONFIG["failure_issue_threshold"] >= 2
    # The margin has to leave room for a check-in that was merely slow.
    assert checkin.MONITOR_CONFIG["checkin_margin"] >= 1
    assert checkin.INTERVAL_S == 60, "the schedule above says one minute"


# -- fail-soft -----------------------------------------------------------------


def test_send_answers_false_and_does_not_sit_on_an_unroutable_host():
    # An unroutable host: send() must answer False rather than raise, and must
    # not sit on it - a lane cannot wait on telemetry.
    t0 = time.time()
    assert checkin.send("http://127.0.0.1:9/api/1/cron/x/y/") is False
    assert time.time() - t0 < checkin.TIMEOUT_S + 5


def test_start_refuses_without_a_dsn_and_under_a_test_run():
    # No DSN and a test run both mean "do not start". Under the test suite
    # ENV is already test, so this is the live rule, not a contrivance.
    assert events.ENV == "test", events.ENV
    assert checkin.start("listener", {}) is None
    assert checkin.start("listener", {"sentryDsn": DSN}) is None, (
        "a test run must never touch a live monitor"
    )


# -- the thread, with the network stubbed --------------------------------------


def test_thread_checks_in_and_logs_the_first_result(monkeypatch):
    sent = []

    def fake_send(url, status="ok"):
        sent.append(url)
        return len(sent) > 1  # first call fails, then recovers

    monkeypatch.setattr(checkin, "send", fake_send)
    monkeypatch.setattr(checkin, "last_ok", None)  # the thread sets it; put it back
    monkeypatch.setattr(events, "ENV", "prod")  # env=test would refuse to start at all
    day = time.strftime("%Y%m%d")
    stream = paths.logs() / f"k15-{day}.jsonl"

    def records():
        try:
            return [
                json.loads(x)
                for x in stream.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
        except OSError:
            return []

    t = checkin.start("listener", {"sentryDsn": DSN})
    assert t is not None and t.daemon, "must not hold the process open"
    # Wait on the EVENT, not on last_ok: the flag is set before the emit.
    for _ in range(300):
        if any(r["event"].startswith("checkin") for r in records()):
            break
        time.sleep(0.01)
    assert sent and sent[0] == URL, sent
    assert checkin.last_ok is False, "the stub failed the first call"

    # Both outcomes are events, and both names are frozen in test_event_names.
    lines = records()
    names = {r["event"] for r in lines}
    assert "checkin_failed" in names, names
    failed = [r for r in lines if r["event"] == "checkin_failed"][0]
    assert failed["lane"] == "listener" and failed["level"] == events.WARN
    assert failed["monitor"] == "k15-listener", failed
