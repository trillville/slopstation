"""Structured events - the JSONL shape, levels, correlation
context, the secret scrubber, daily rollover, and fail-soft.
"""

import json
import os
import subprocess
import sys
import time

from slopstation import events, logbook, paths

# One emitter process; SLOPSTATION_HOME in its environment points it at the
# test's own tree.
WORKER_SRC = """import sys
from slopstation import events
# Wait for the lock as long as it takes: this proves the lock makes the
# seek+write pair atomic. Production caps the wait at LOCK_WAIT_S and then
# writes unlocked - a lost line beats a blocked lane - which on a loaded CI
# runner is exactly what lost lines here.
events.LOCK_WAIT_S = 30
for _ in range(120):
    events.emit('supervisor', 'restart', what=sys.argv[1], code=-1)
"""


def read(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def daily_files():
    """This test's daily files, oldest first."""
    return sorted(paths.logs().glob("test-*.jsonl"))


# -- the record shape ------------------------------------------------------


def test_record_shape():
    r = events.emit("voice", "wake", score=0.71)
    assert r["lane"] == "voice" and r["event"] == "wake" and r["score"] == 0.71
    assert r["level"] == "info" and r["service"] == "k15"
    assert r["ts"].endswith("Z") and "T" in r["ts"], r["ts"]
    # env auto-detects as test, so nothing here reads as production traffic.
    assert r["env"] == "test", f"env={r['env']} - auto-detection missed the suite"

    files = list(paths.logs().glob("*.jsonl"))
    assert len(files) == 1, files
    assert files[0].name.startswith("test-"), (
        f"{files[0].name} - test events must not land in the shipped file"
    )
    assert read(files[0])[0]["event"] == "wake"


def test_none_is_absence_not_a_value():
    # None is absence, not a value: an inapplicable field must not emit null.
    r = events.emit("launch", "host_ready", status="READY", appid=None)
    assert "appid" not in r and r["status"] == "READY"


# -- levels ----------------------------------------------------------------


def test_levels():
    # level is positional-only, so it is passed positionally, as logbook.Logger does.
    r = events.emit("launch", "launch_failed", events.ERROR, err="boom")
    assert r["level"] == "error"
    assert events.human("launch_failed", events.ERROR, err="boom").startswith(
        "ERROR "
    ), "errors must be visible in the console line"
    assert not events.human("wake", score=0.7).startswith("INFO"), (
        "info is the default and should not shout"
    )


# -- a caller field may be named ANYTHING ----------------------------------


def test_a_caller_field_may_be_named_anything():
    # A colliding caller kwarg raises TypeError at argument BINDING, before any
    # try/except inside emit - hence positional-only parameters, emitter keys
    # winning, and the caller's value kept under f_*.
    log = logbook.CapturingLog("voice")
    for name in (
        "ts",
        "level",
        "env",
        "service",
        "lane",
        "event",
        "host",
        "turn",
        "session",
        "job",
        "dur_ms",
        "err",
        "msg",
        "self",
    ):
        r = events.emit("voice", "collide", events.INFO, **{name: "X"})
        assert r is not None, f"emit() died on a field named {name!r}"
        assert r["lane"] == "voice" and r["event"] == "collide", (
            f"field {name!r} clobbered an emitter-owned key: {r}"
        )
        assert "X" in r.values(), f"field {name!r} was dropped silently: {r}"
        log(f"collide_{name}", **{name: "X"})
    assert len(log.records) == 14, log.records


# -- correlation context ---------------------------------------------------


def test_correlation_context():
    tok = events.context(turn="9f2c1a", session="3b7e")
    r = events.emit("voice", "gate_match", intent="PlayGame")
    assert r["turn"] == "9f2c1a" and r["session"] == "3b7e"
    # Explicit beats ambient, without disturbing the caller's context.
    r = events.emit("voice", "gate_match", turn="deadbe")
    assert r["turn"] == "deadbe" and r["session"] == "3b7e"
    # Correlation ids are for the machine; the human line stays readable.
    assert "9f2c1a" not in events.human("gate_match", turn="9f2c1a", verb="play")
    events.reset(tok)
    assert "turn" not in events.emit("voice", "wake")


# -- the scrubber ----------------------------------------------------------


def test_scrubber(monkeypatch):
    monkeypatch.setattr(
        events, "_redactions", {"sk-ant-supersecretvalue123", "dg_realkeyvalue4567890"}
    )
    r = events.emit("voice", "lane_up", note="using sk-ant-supersecretvalue123 now")
    assert "supersecret" not in json.dumps(r), r
    assert "***" in r["note"]
    # By field name too: a key that never reached secrets.json is still caught.
    r = events.emit("voice", "lane_up", apiKey="anything-at-all", token="xyz")
    assert r["apiKey"] == "***" and r["token"] == "***", r
    # The human line crosses the same boundary - consoles get screenshotted.
    assert "supersecret" not in events.human("x", note="sk-ant-supersecretvalue123")


# -- fail-soft -------------------------------------------------------------


def test_an_unwritable_log_dir_loses_the_write_not_the_event(monkeypatch, tmp_path):
    # Unwritable dir (parent is a file) must not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "logs", lambda: blocker / "sub")
    monkeypatch.setattr(events, "_last_day", None)  # the mkdir is the day's first
    assert events.emit("voice", "wake") is not None  # record built, write lost


def test_an_unserializable_value_costs_the_field_not_the_event():
    # An unserializable value costs the field's fidelity, never the event.
    r = events.emit("voice", "wake", weird=object())
    assert r is not None and isinstance(read(daily_files()[-1])[-1]["weird"], str)


# -- rollover + retention --------------------------------------------------


def test_rollover_prunes_expired_daily_files(monkeypatch):
    paths.logs().mkdir()
    old = paths.logs() / "test-20200101.jsonl"
    old.write_text('{"event":"ancient"}\n', encoding="utf-8")
    stale = time.time() - (events.TTL_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    monkeypatch.setattr(events, "_last_day", None)  # force a rollover pass
    events.emit("voice", "wake")
    assert not old.exists(), "expired daily file survived the rollover prune"


# -- heartbeat: the signal absence is measured against ---------------------


def test_heartbeat():
    beater = events.start_heartbeat("listener", interval_s=0.05)
    time.sleep(0.3)
    # Daemon, or a dead agent hangs on exit instead of dying cleanly.
    assert beater.is_alive() and beater.daemon
    beater.stop.set()
    beater.join(timeout=2)
    assert not beater.is_alive(), "the heartbeat thread did not stop"
    beats = [r for r in read(daily_files()[-1]) if r["event"] == "heartbeat"]
    assert len(beats) >= 3, f"only {len(beats)} beats in 0.3s at 50ms"
    assert beats[0]["lane"] == "listener"


# -- the CLI smart-alert.bat calls -----------------------------------------


def test_cli():
    assert (
        events._cli(
            [
                "emit",
                "supervisor",
                "restart",
                "what=listener",
                "code=3",
                "--level",
                "warn",
            ]
        )
        == 0
    )
    # By event, not [-1]: the lookup does not care what else lands in the file.
    rec = [r for r in read(daily_files()[-1]) if r["event"] == "restart"][-1]
    assert rec["level"] == "warn"
    assert rec["code"] == 3, f"cmd.exe text must land as a number, got {rec['code']!r}"
    assert events._cli(["nonsense"]) == 2, "a bad CLI call must not pretend to work"


# -- concurrent emitters keep every line -----------------------------------


def test_concurrent_emitters_keep_every_line(tmp_path):
    # Windows appends by seek-then-write, so two processes racing for the
    # same offset silently overwrite one another. A reload emits while the
    # supervisor it just bounced also emits: that is where this was found.
    race = tmp_path / "race"
    race.mkdir()
    worker = race / "emit_worker.py"
    worker.write_text(WORKER_SRC, encoding="utf-8")
    env = dict(os.environ, SLOPSTATION_HOME=str(race))
    procs = [
        subprocess.Popen([sys.executable, str(worker), "w" * 8], env=env)
        for _ in range(6)
    ]
    for proc in procs:
        proc.wait()
    written = list((race / "logs").glob("*.jsonl"))
    assert len(written) == 1, written
    lines = [
        line
        for line in written[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for line in lines:
        json.loads(line)  # a torn line raises here
    assert len(lines) == 720, f"lost {720 - len(lines)} lines to the race"


# -- the shared test double keeps the production shape ---------------------


def test_capturing_log_keeps_the_production_shape():
    cap = logbook.CapturingLog("voice")
    cap("wake", score=0.5)
    cap.warn("earcon_failed", err="x")
    cap.error("pipeline_error", err="y")
    assert cap.events() == ["wake", "earcon_failed", "pipeline_error"]
    assert cap.find("earcon_failed")[0]["level"] == "warn"
    assert cap.find("pipeline_error")[0]["level"] == "error"
