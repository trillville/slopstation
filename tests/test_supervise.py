"""The lane wrapper and the task verbs: the install gate runs before the lane
and re-checks under its lock, reconcile runs once per boot, an elevated window
is refused, and start()/deploy speak schtasks."""

import subprocess
import types

import pytest

from helpers import fresh_state
from slopstation import supervise


@pytest.fixture(autouse=True)
def _normal_window(monkeypatch):
    monkeypatch.setattr(supervise, "elevated", lambda: False)


def _completed(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_lane_installs_then_runs_and_returns_the_exit_code(monkeypatch, tmp_path):
    fresh_state()
    calls = []

    def fake_run(argv, **kw):
        calls.append("pip" if "pip" in argv else argv[-1])
        return _completed(returncode=0 if "pip" in argv else 3)

    monkeypatch.setattr(supervise, "SENTINEL", tmp_path / "deps-ok")
    monkeypatch.setattr(
        supervise, "pins_changed", lambda: not (tmp_path / "deps-ok").exists()
    )
    monkeypatch.setattr(supervise, "_pin_digest", lambda: "digest")
    monkeypatch.setattr(supervise, "_first_launch_this_boot", lambda: False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert supervise.lane("voice", ["--once"]) == 3
    assert calls == ["pip", "--once"]
    assert (tmp_path / "deps-ok").read_text() == "digest"


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
