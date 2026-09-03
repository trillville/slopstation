"""Doctor.py's checks with every probe stubbed (serial, hid,
process list, ssh, sc). Asserts the row names and the levels that matter;
the hints are prose.
"""

import json
import subprocess
import time
import types

import hid
import pytest
import serial

import helpers
from slopstation import config, doctor, gamepc, paths, sessionlock, statefile, supervise


class _Serial:
    """serial.Serial that opens every port but COMNONE."""

    def __init__(self, port, baud, timeout=1):
        if port == "COMNONE":
            raise OSError("no such port")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_run(argv, **kw):
    """subprocess.run for the process list and `sc query`."""
    out = ""
    if argv[0] == "sc":
        out = "STATE : 4 RUNNING"
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


@pytest.fixture(autouse=True)
def _probes(monkeypatch):
    """The checks open the Ex-Link port, enumerate the Puck, shell out for
    `sc query` and read git's HEAD; none of that is on a dev box."""
    monkeypatch.setattr(serial, "Serial", _Serial)
    monkeypatch.setattr(
        hid, "enumerate", lambda vid, pid: [{"path": b"a"}, {"path": b"b"}]
    )
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(doctor, "_local_rev", lambda: "abc1234")


class _Rows(list):
    """Every report() call of the test as (level, name, detail)."""

    def levels(self):
        return {name: level for level, name, _ in self}

    def names(self):
        return {name for _, name, _ in self}

    def detail(self, name):
        return next(detail for _, n, detail in self if n == name)


@pytest.fixture
def rows(monkeypatch):
    out = _Rows()
    monkeypatch.setattr(
        doctor,
        "report",
        lambda level, name, detail, hint="": out.append((level, name, detail)),
    )
    return out


@pytest.fixture
def cfg(monkeypatch):
    """config.example.json, and what config.current() answers (check_ssh
    reads sshHost from it)."""
    cfg = dict(helpers.CONFIG)
    monkeypatch.setattr(config, "_current", cfg)
    return cfg


@pytest.fixture
def media_cfg(cfg):
    """The example config with the media lane on - a deep copy, since the
    tests delete keys from it."""
    media_cfg = json.loads(json.dumps(cfg))
    media_cfg["media"]["enabled"] = True
    return media_cfg


@pytest.fixture
def lanes_down(monkeypatch):
    """Both lane tasks registered but not running."""
    monkeypatch.setattr(
        supervise, "query", lambda lane: {"Status": "Ready", "Last Result": "1"}
    )


@pytest.fixture
def media_up(monkeypatch):
    """Both *arr keys in secrets.json and every sidecar answering its port."""
    monkeypatch.setattr(
        config,
        "secrets",
        lambda: {"radarrApiKey": "r" * 32, "sonarrApiKey": "s" * 32},
    )
    monkeypatch.setattr(doctor, "_tcp_reachable", lambda url, timeout=1: True)


# --- config --------------------------------------------------------------


def test_config_passes_with_every_required_key(rows, cfg, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: cfg)
    got = doctor.check_config()
    assert got is cfg and rows.levels()["config.json"] == "PASS"


def test_config_fails_on_a_missing_required_key(rows, cfg, monkeypatch):
    monkeypatch.setattr(
        config, "load", lambda: {k: v for k, v in cfg.items() if k != "sshHost"}
    )
    assert doctor.check_config() is not None and rows.levels()["config.json"] == "FAIL"


# --- imports, serial, puck ---------------------------------------------------


def test_imports(rows):
    doctor.check_imports()
    assert rows.levels()["import serial"] == "PASS"
    assert rows.levels()["import hid"] == "PASS"


def test_ex_link_port_opens_or_fails(rows):
    doctor.check_com({"tvComPort": "COM3"})
    doctor.check_com({"tvComPort": "COMNONE"})
    com = [level for level, n, _ in rows if n == "ex-link port"]
    assert com == ["PASS", "FAIL"], com


def test_puck_enumerates_or_fails(rows, monkeypatch):
    assert doctor.check_puck() is True and rows.levels()["puck"] == "PASS"
    monkeypatch.setattr(hid, "enumerate", lambda vid, pid: [])
    assert doctor.check_puck() is False


# --- listener ------------------------------------------------------------


def test_listener_task_running_or_not(rows, monkeypatch):
    monkeypatch.setattr(supervise, "query", lambda lane: {"Status": "Running"})
    assert doctor.check_listener() is True and rows.levels()["listener"] == "PASS"
    monkeypatch.setattr(
        supervise, "query", lambda lane: {"Status": "Ready", "Last Result": "1"}
    )
    assert doctor.check_listener() is False and rows.levels()["listener"] == "WARN"


# --- ssh: status, DENIED probe, deploy skew ----------------------------------


def _fake_ssh(cmd, timeout=15):
    if cmd == "status":
        return "NOTREADY"
    if cmd == "bogus":
        raise subprocess.CalledProcessError(1, "ssh", output="DENIED\n")
    if cmd == "version":
        return "abc1234 2026-08-22"
    raise AssertionError(cmd)


def test_ssh_status_dispatch_and_deploy_skew(rows, cfg, monkeypatch):
    monkeypatch.setattr(gamepc, "ssh", _fake_ssh)
    doctor.check_ssh()
    lv = rows.levels()
    assert (
        lv["ssh status"] == "PASS"
        and lv["ssh dispatch"] == "PASS"
        and lv["deploy skew"] == "PASS"
    ), lv


def test_deploy_skew_warns_on_a_dirty_build(rows, cfg, monkeypatch):
    def dirty_version(cmd, timeout=15):
        return (
            "abc1234-dirty 2026-08-22" if cmd == "version" else _fake_ssh(cmd, timeout)
        )

    monkeypatch.setattr(gamepc, "ssh", dirty_version)
    doctor.check_ssh()
    assert rows.levels()["deploy skew"] == "WARN"


# --- session state ---------------------------------------------------------


def test_session_state_idle(rows):
    doctor.check_session_state()
    assert rows.levels()["session lock"] == "PASS"
    assert rows.levels()["last_error"] == "PASS"


def test_session_state_fresh_lock_and_a_last_error(rows):
    helpers.seed_lock(10)
    sessionlock.last_error_file().write_text("boom")
    doctor.check_session_state()
    assert rows.levels()["session lock"] == "PASS"
    assert rows.levels()["last_error"] == "WARN"


def test_session_state_stale_lock(rows):
    helpers.seed_lock(sessionlock.LOCK_STALE_S + 1)
    doctor.check_session_state()
    assert rows.levels()["session lock"] == "WARN"


# --- telemetry -------------------------------------------------------------


def test_telemetry(rows):
    doctor.check_telemetry()
    # Nothing has written into this test's log directory.
    assert rows.levels()["event stream"] == "WARN"
    assert rows.levels()["log shipper"] == "PASS"


# --- voice (filesystem + process checks only) ----------------------------------


def test_voice_rows_without_keys_or_a_running_agent(rows, cfg, lanes_down, monkeypatch):
    monkeypatch.setattr(config, "secrets", lambda: {})
    doctor.check_voice(cfg)
    names = rows.names()
    assert {
        "voice keys",
        "venv",
        "voice library",
        "voice agent",
        "operations",
        "media",
    } <= names, names
    lv = rows.levels()
    assert lv["voice keys"] == "WARN" and lv["voice agent"] == "WARN"
    assert lv["operations"] == "PASS"
    assert lv["media"] == "PASS"


def test_operations_in_an_unknown_state_with_the_agent_down(rows, lanes_down):
    statefile.write(
        paths.state() / "operations.json",
        [{"id": "op-test", "state": "UNKNOWN", "announcement_pending": False}],
    )
    doctor.check_operations()
    assert rows.levels()["operations"] == "WARN"


def test_voice_without_a_voice_section(rows):
    doctor.check_voice({})
    assert rows.levels()["voice config"] == "WARN"


# --- media -----------------------------------------------------------------


def test_media_fully_configured(rows, media_cfg, media_up):
    doctor.check_media(media_cfg)
    lv = rows.levels()
    assert lv["media config"] == "PASS"
    assert lv["media keys"] == "PASS"
    assert lv["media services"] == "PASS"


def test_media_names_the_unconfigured_service(rows, media_cfg, media_up):
    del media_cfg["media"]["prowlarrUrl"]
    doctor.check_media(media_cfg)
    lv = rows.levels()
    assert lv["media config"] == "WARN"
    assert lv["media services"] == "WARN"
    assert "unconfigured: Prowlarr" in rows.detail("media services")


# --- monitored-and-missing outside active work ---------------------------


def _wanted_missing():
    """Sonarr's wanted/missing page: four monitored episodes, one per case
    the row tells apart."""
    old_aired = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400)
    )
    fresh_aired = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "records": [
            # Owned by the Andor operation the tests record: not drift.
            {
                "seriesId": 7,
                "seasonNumber": 1,
                "airDateUtc": old_aired,
                "series": {"title": "Andor"},
            },
            # Aired tonight: still in flight, not drift.
            {
                "seriesId": 3,
                "seasonNumber": 18,
                "airDateUtc": fresh_aired,
                "series": {"title": "Sunny"},
            },
            # Aired years ago, nothing chasing it: the surprise-download hole.
            {
                "seriesId": 3,
                "seasonNumber": 1,
                "airDateUtc": old_aired,
                "series": {"title": "Sunny"},
            },
            {
                "seriesId": 3,
                "seasonNumber": 2,
                "airDateUtc": old_aired,
                "series": {"title": "Sunny"},
            },
        ]
    }


def _series_op(external_ref, seasons):
    return {
        "kind": "series_acquisition",
        "state": "RUNNING",
        "external_ref": external_ref,
        "metadata": {"seasons": seasons},
    }


@pytest.fixture
def sonarr_wanted(monkeypatch, media_up):
    monkeypatch.setattr(
        doctor,
        "_arr_get",
        lambda url, key, path, params=None, timeout=4: _wanted_missing(),
    )


def test_media_monitoring_flags_episodes_nobody_is_chasing(
    rows, media_cfg, sonarr_wanted
):
    statefile.write(paths.state() / "operations.json", [_series_op("7", [1])])
    doctor.check_media_monitoring(media_cfg)
    assert rows.levels()["media monitoring"] == "WARN"
    detail = rows.detail("media monitoring")
    assert "2 episode(s)" in detail and "Sunny (2)" in detail, detail


def test_media_monitoring_whole_series_operation_owns_every_season(
    rows, media_cfg, sonarr_wanted
):
    # A whole-series operation (seasons: null) accounts for every season of
    # it, so with both series owned nothing is left armed.
    statefile.write(
        paths.state() / "operations.json",
        [_series_op("7", [1]), _series_op("3", None)],
    )
    doctor.check_media_monitoring(media_cfg)
    assert rows.levels()["media monitoring"] == "PASS"


def test_media_monitoring_without_a_sonarr_key(rows, media_cfg, monkeypatch):
    monkeypatch.setattr(config, "secrets", lambda: {})
    doctor.check_media_monitoring(media_cfg)
    assert rows.levels()["media monitoring"] == "WARN"
