"""The lane wrapper and the task verbs: the install gate runs before the lane
and re-checks under its lock, reconcile runs once per boot, an elevated window
is refused, and start()/deploy speak schtasks."""

import subprocess
import time
import types

import pytest

from helpers import fresh_state
from slopstation import supervise


@pytest.fixture(autouse=True)
def _normal_window(monkeypatch):
    monkeypatch.setattr(supervise, "elevated", lambda: False)


def _completed(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class Stop(Exception):
    pass


def _lane_seams(monkeypatch, tmp_path, lane_code):
    calls = []

    def fake_run(argv, **kw):
        calls.append("pip" if "pip" in argv else argv[-1])
        return _completed(returncode=0 if "pip" in argv else lane_code)

    monkeypatch.setattr(supervise, "SENTINEL", tmp_path / "deps-ok")
    monkeypatch.setattr(
        supervise, "pins_changed", lambda: not (tmp_path / "deps-ok").exists()
    )
    monkeypatch.setattr(supervise, "_pin_digest", lambda: "digest")
    monkeypatch.setattr(supervise, "_first_launch_this_boot", lambda: False)
    monkeypatch.setattr(supervise, "_die_together", lambda: None)  # not pytest's job
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_the_job_object_can_be_made():
    # Made and closed with nothing in it; joining it would make pytest itself
    # die with the test process.
    job = supervise._kill_on_close_job()
    assert job
    assert supervise.ctypes.windll.kernel32.CloseHandle(job)


def test_lane_installs_then_runs_and_restarts_a_crash(monkeypatch, tmp_path):
    fresh_state()
    calls = _lane_seams(monkeypatch, tmp_path, lane_code=3)

    def backoff(_s):
        raise Stop  # the restart pause is where the test gets off

    monkeypatch.setattr(time, "sleep", backoff)
    with pytest.raises(Stop):
        supervise.lane("voice", ["--once"])
    assert calls == ["pip", "--once"]
    assert (tmp_path / "deps-ok").read_text() == "digest"


def test_lane_ends_with_a_clean_exit(monkeypatch, tmp_path):
    fresh_state()
    (tmp_path / "deps-ok").write_text("digest")
    calls = _lane_seams(monkeypatch, tmp_path, lane_code=0)
    assert supervise.lane("voice", ["--once"]) == 0
    assert calls == ["--once"]


def test_listener_reconciles_once_per_boot(monkeypatch):
    fresh_state()
    monkeypatch.setattr(supervise, "_uptime_s", lambda: 1000.0)
    assert supervise._first_launch_this_boot() is True
    assert supervise._first_launch_this_boot() is False  # a restart, same boot
    monkeypatch.setattr(supervise, "_uptime_s", lambda: 5.0)  # rebooted since
    assert supervise._first_launch_this_boot() is True


def test_lane_refuses_an_elevated_window(monkeypatch):
    monkeypatch.setattr(supervise, "elevated", lambda: True)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    assert supervise.lane("listener", []) == 1
    assert supervise.start() == 1
    assert ran == []


def test_installer_rechecks_under_the_lock(monkeypatch, tmp_path):
    # Changed at the door, installed by the time we hold the lock.
    answers = iter([True, False])
    ran = []
    monkeypatch.setattr(supervise, "SENTINEL", tmp_path / "deps-ok")
    monkeypatch.setattr(supervise, "pins_changed", lambda: next(answers))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    supervise._install_if_pins_changed(lambda *a, **k: None)
    assert ran == []


CSV = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result"\r\n'
    '"K15","\\Slopstation\\listener","N/A","Running","Interactive only","9/3/2026 1:31:20 AM","267009"\r\n'
)


def test_query_reads_the_task_row(monkeypatch):
    seen = []

    def fake(*args):
        seen.append(args)
        if args[0] == "/Query" and "listener" in args[2]:
            return _completed(CSV)
        return _completed(returncode=1)

    monkeypatch.setattr(supervise, "_schtasks", fake)
    row = supervise.query("listener")
    assert row["Status"] == "Running" and row["Last Result"] == "267009"
    assert supervise.running("listener") is True
    assert supervise.query("voice") is None  # not registered
    assert supervise.running("voice") is False
    assert seen[0] == ("/Query", "/TN", "\\Slopstation\\listener", "/FO", "CSV", "/V")


def test_start_reloads_what_runs_and_starts_what_does_not(monkeypatch):
    status = {"listener": "Running", "voice": "Ready"}
    verbs = []
    monkeypatch.setattr(supervise, "query", lambda lane: {"Status": status[lane]})

    def stop(lane):
        verbs.append(("End", lane))
        status[lane] = "Ready"
        return True

    def run(lane):
        verbs.append(("Run", lane))
        return True

    monkeypatch.setattr(supervise, "stop", stop)
    monkeypatch.setattr(supervise, "run", run)
    assert supervise.start() == 0
    assert verbs == [("End", "listener"), ("Run", "listener"), ("Run", "voice")]


def test_start_needs_the_tasks_registered(monkeypatch):
    monkeypatch.setattr(supervise, "query", lambda lane: None)
    assert supervise.start() == 1


def test_stop_refuses_to_claim_a_task_that_did_not_end(monkeypatch):
    # A quiet True here is a deploy that /Runs into IgnoreNew, then takes the
    # old instance for its relaunch and reports success with the old code up.
    monkeypatch.setattr(supervise, "STOP_WAIT_S", 0.05)
    monkeypatch.setattr(
        supervise, "_schtasks", lambda *a: _completed(CSV)
    )  # Running, always
    with pytest.raises(RuntimeError, match="still running"):
        supervise.stop("listener")


def test_run_raises_when_the_scheduler_refuses(monkeypatch):
    monkeypatch.setattr(supervise, "_schtasks", lambda *a: _completed("", returncode=1))
    with pytest.raises(RuntimeError, match="/Run"):
        supervise.run("voice")


def test_start_fails_loudly_when_a_reload_does_not_stop(monkeypatch):
    monkeypatch.setattr(supervise, "query", lambda lane: {"Status": "Running"})

    def stop(lane):
        raise RuntimeError("still running")

    monkeypatch.setattr(supervise, "stop", stop)
    assert supervise.start() == 1
