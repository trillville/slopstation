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
from helpers import fresh_state
from slopstation import cglib, doctor, gamepc, supervise


class _Serial:
    """serial.Serial that opens every port but COMNONE."""

    def __init__(self, port, baud, timeout=1):
        if port == "COMNONE":
            raise OSError("no such port")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _devices(monkeypatch):
    """The checks open the Ex-Link port and enumerate the Puck; neither is on
    a dev box."""
    monkeypatch.setattr(serial, "Serial", _Serial)
    monkeypatch.setattr(
        hid, "enumerate", lambda vid, pid: [{"path": b"a"}, {"path": b"b"}]
    )


rows = []


def capture(level, name, detail, hint=""):
    rows.append((level, name, detail))


def levels():
    return {name: level for level, name, _ in rows}


def fake_run(argv, **kw):
    """subprocess.run for the process list and `sc query`."""
    out = ""
    if argv[0] == "sc":
        out = "STATE : 4 RUNNING"
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


def test_doctor(monkeypatch):
    doctor.report = capture
    doctor.subprocess.run = fake_run
    doctor._local_rev = lambda: "abc1234"
    cfg = dict(helpers.CONFIG)

    # --- config --------------------------------------------------------------
    cglib.load_config = lambda: cfg
    got = doctor.check_config()
    assert got is cfg and levels()["config.json"] == "PASS"
    rows.clear()
    cglib.load_config = lambda: {k: v for k, v in cfg.items() if k != "sshHost"}
    assert doctor.check_config() is not None and levels()["config.json"] == "FAIL"
    rows.clear()

    # --- imports, serial, puck ---------------------------------------------------
    doctor.check_imports()
    doctor.check_com({"tvComPort": "COM3"})
    doctor.check_com({"tvComPort": "COMNONE"})
    assert levels()["import serial"] == "PASS" and levels()["import hid"] == "PASS"
    com = [level for level, n, _ in rows if n == "ex-link port"]
    assert com == ["PASS", "FAIL"], com
    assert doctor.check_puck() is True and levels()["puck"] == "PASS"
    monkeypatch.setattr(hid, "enumerate", lambda vid, pid: [])
    assert doctor.check_puck() is False
    monkeypatch.setattr(hid, "enumerate", lambda vid, pid: [{"path": b"a"}])
    rows.clear()

    # --- listener ------------------------------------------------------------
    monkeypatch.setattr(supervise, "query", lambda lane: {"Status": "Running"})
    assert doctor.check_listener() is True and levels()["listener"] == "PASS"
    monkeypatch.setattr(
        supervise, "query", lambda lane: {"Status": "Ready", "Last Result": "1"}
    )
    assert doctor.check_listener() is False and levels()["listener"] == "WARN"
    rows.clear()

    # --- ssh: status, DENIED probe, deploy skew ----------------------------------
    def fake_ssh(cmd, timeout=15):
        if cmd == "status":
            return "NOTREADY"
        if cmd == "bogus":
            raise subprocess.CalledProcessError(1, "ssh", output="DENIED\n")
        if cmd == "version":
            return "abc1234 2026-08-22"
        raise AssertionError(cmd)

    gamepc.ssh = fake_ssh
    doctor.check_ssh()
    lv = levels()
    assert (
        lv["ssh status"] == "PASS"
        and lv["ssh dispatch"] == "PASS"
        and lv["deploy skew"] == "PASS"
    ), lv
    rows.clear()

    def dirty_version(cmd, timeout=15):
        return (
            "abc1234-dirty 2026-08-22" if cmd == "version" else fake_ssh(cmd, timeout)
        )

    gamepc.ssh = dirty_version
    doctor.check_ssh()
    assert levels()["deploy skew"] == "WARN"
    rows.clear()

    # --- session state ---------------------------------------------------------
    fresh_state()
    doctor.check_session_state()
    assert levels()["session lock"] == "PASS" and levels()["last_error"] == "PASS"
    rows.clear()
    fresh_state(lock_age_s=10)
    cglib.LAST_ERROR.write_text("boom")
    doctor.check_session_state()
    assert levels()["session lock"] == "PASS" and levels()["last_error"] == "WARN"
    rows.clear()
    fresh_state(lock_age_s=cglib.LOCK_STALE_S + 1)
    doctor.check_session_state()
    assert levels()["session lock"] == "WARN"
    rows.clear()

    # --- telemetry -------------------------------------------------------------
    doctor.check_telemetry()
    assert levels()["event stream"] == "WARN"  # nothing written in the tmp LOG_DIR
    assert levels()["log shipper"] == "PASS"
    rows.clear()

    # --- voice (filesystem + process checks only) ----------------------------------
    cglib.load_secrets = lambda: {}
    doctor.check_voice(cfg)
    names = {n for _, n, _ in rows}
    assert {
        "voice keys",
        "venv",
        "voice library",
        "voice agent",
        "operations",
        "media",
    } <= names, names
    assert levels()["voice keys"] == "WARN" and levels()["voice agent"] == "WARN"
    assert levels()["operations"] == "PASS"
    assert levels()["media"] == "PASS"
    rows.clear()
    cglib.write_json(
        cglib.STATE / "operations.json",
        [{"id": "op-test", "state": "UNKNOWN", "announcement_pending": False}],
    )
    doctor.check_operations()
    assert levels()["operations"] == "WARN"
    rows.clear()
    doctor.check_voice({})
    assert levels()["voice config"] == "WARN"
    rows.clear()

    media_cfg = json.loads(json.dumps(cfg))
    media_cfg["media"]["enabled"] = True
    cglib.load_secrets = lambda: {"radarrApiKey": "r" * 32, "sonarrApiKey": "s" * 32}
    doctor._tcp_reachable = lambda url, timeout=1: True
    doctor.check_media(media_cfg)
    assert levels()["media config"] == "PASS"
    assert levels()["media keys"] == "PASS"
    assert levels()["media services"] == "PASS"
    rows.clear()
    del media_cfg["media"]["prowlarrUrl"]
    doctor.check_media(media_cfg)
    assert levels()["media config"] == "WARN"
    assert levels()["media services"] == "WARN"
    service_detail = next(
        detail for _, name, detail in rows if name == "media services"
    )
    assert "unconfigured: Prowlarr" in service_detail
    rows.clear()

    # --- monitored-and-missing outside active work ---------------------------
    media_cfg = json.loads(json.dumps(cfg))
    media_cfg["media"]["enabled"] = True
    old_aired = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400)
    )
    fresh_aired = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wanted = {
        "records": [
            # Owned by the active operation below: not drift.
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
    doctor._arr_get = lambda url, key, path, params=None, timeout=4: wanted
    cglib.write_json(
        cglib.STATE / "operations.json",
        [
            {
                "kind": "series_acquisition",
                "state": "RUNNING",
                "external_ref": "7",
                "metadata": {"seasons": [1]},
            }
        ],
    )
    doctor.check_media_monitoring(media_cfg)
    assert levels()["media monitoring"] == "WARN"
    detail = next(d for _, n, d in rows if n == "media monitoring")
    assert "2 episode(s)" in detail and "Sunny (2)" in detail, detail
    rows.clear()
    # A whole-series operation (seasons: null) accounts for every season of
    # it, so with both series owned nothing is left armed.
    cglib.write_json(
        cglib.STATE / "operations.json",
        [
            {
                "kind": "series_acquisition",
                "state": "RUNNING",
                "external_ref": "7",
                "metadata": {"seasons": [1]},
            },
            {
                "kind": "series_acquisition",
                "state": "RUNNING",
                "external_ref": "3",
                "metadata": {"seasons": None},
            },
        ],
    )
    doctor.check_media_monitoring(media_cfg)
    assert levels()["media monitoring"] == "PASS"
    rows.clear()
    cglib.load_secrets = lambda: {}
    doctor.check_media_monitoring(media_cfg)
    assert levels()["media monitoring"] == "WARN"
    rows.clear()
