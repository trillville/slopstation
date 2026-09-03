"""The supervisor's restart loop: the dependency gate runs before EVERY launch,
so a deploy that lands new pins and then kills the lane installs them on the
relaunch, and an installer re-checks the sentinel once it holds the lock, so
the second of two supervisors starting together does not install again."""

import subprocess
import time
import types

import pytest

from slopstation import supervise


class Stop(Exception):
    pass


def test_gate_runs_before_each_launch(monkeypatch, tmp_path):
    state = {"changed": False}
    calls = []

    def fake_run(argv, **kw):
        calls.append("pip" if "pip" in argv else "lane")
        # The first launch returns to a world where a deploy landed new pins;
        # the install clears that.
        state["changed"] = "pip" not in argv
        return types.SimpleNamespace(returncode=0)

    def fake_sleep(_s):
        if calls.count("lane") == 2:
            raise Stop

    monkeypatch.setattr(supervise, "SENTINEL", tmp_path / "deps-ok")
    monkeypatch.setattr(supervise, "pins_changed", lambda: state["changed"])
    monkeypatch.setattr(supervise, "_pin_digest", lambda: "digest")
    monkeypatch.setattr(supervise, "_hold", lambda lane: object())
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        supervise.supervise("voice", [])
    assert calls == ["lane", "pip", "lane"]
    assert (tmp_path / "deps-ok").read_text() == "digest"


def test_installer_rechecks_under_the_lock(monkeypatch, tmp_path):
    answers = iter(
        [True, False]
    )  # changed at the door, installed by the time we hold the lock
    ran = []
    monkeypatch.setattr(supervise, "SENTINEL", tmp_path / "deps-ok")
    monkeypatch.setattr(supervise, "pins_changed", lambda: next(answers))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    supervise._install_if_pins_changed(lambda *a, **k: None)
    assert ran == []
