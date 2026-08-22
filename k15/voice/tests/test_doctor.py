"""Blind test: doctor.py's checks with every probe stubbed (serial, hid,
process list, ssh, sc). Asserts the row names and the levels that matter;
the hints are prose. Run:
    .venv\\Scripts\\python tests\\test_doctor.py
"""
import subprocess
import sys
import types

import _bootstrap                               # noqa: F401,E402
from _bootstrap import fresh_state              # noqa: E402

import cglib                                    # noqa: E402
import gamepc                                   # noqa: E402

# serial / hid are imported inside the checks; stub both before doctor loads.
serial = types.ModuleType("serial")


class _Serial:
    def __init__(self, port, baud, timeout=1):
        if port == "COMNONE":
            raise OSError("no such port")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


serial.Serial = _Serial
hid = types.ModuleType("hid")
hid.enumerate = lambda vid, pid: [{"path": b"a"}, {"path": b"b"}]
sys.modules["serial"] = serial
sys.modules["hid"] = hid

import doctor                                   # noqa: E402

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


def main():
    doctor.report = capture
    doctor.subprocess.run = fake_run
    doctor._local_rev = lambda: "abc1234"
    doctor._worker_exe = lambda voice_dir, wp: sys.executable
    cfg = dict(_bootstrap.CONFIG)

    # --- config --------------------------------------------------------------
    cglib.load_config = lambda: cfg
    got = doctor.check_config()
    assert got is cfg and levels()["config.json"] == "PASS"
    rows.clear()
    cglib.load_config = lambda: {k: v for k, v in cfg.items() if k != "sshHost"}
    assert doctor.check_config() is not None and levels()["config.json"] == "FAIL"
    rows.clear()
    print("  config: the one REQUIRED_CONFIG list decides PASS/FAIL")

    # --- imports, serial, puck ---------------------------------------------------
    doctor.check_imports()
    doctor.check_com({"tvComPort": "COM3"})
    doctor.check_com({"tvComPort": "COMNONE"})
    assert levels()["import serial"] == "PASS" and levels()["import hid"] == "PASS"
    com = [l for l, n, _ in rows if n == "ex-link port"]
    assert com == ["PASS", "FAIL"], com
    assert doctor.check_puck() is True and levels()["puck"] == "PASS"
    hid.enumerate = lambda vid, pid: []
    assert doctor.check_puck() is False
    hid.enumerate = lambda vid, pid: [{"path": b"a"}]
    rows.clear()
    print("  hardware: serial open, puck enumeration")

    # --- listener ------------------------------------------------------------
    doctor._python_cmdlines = lambda: "python chord_listener.py"
    assert doctor.check_listener() is True and levels()["listener"] == "PASS"
    doctor._python_cmdlines = lambda: ""
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
    assert lv["ssh status"] == "PASS" and lv["ssh dispatch"] == "PASS" and lv["deploy skew"] == "PASS", lv
    rows.clear()

    def dirty_version(cmd, timeout=15):
        return "abc1234-dirty 2026-08-22" if cmd == "version" else fake_ssh(cmd, timeout)
    gamepc.ssh = dirty_version
    doctor.check_ssh()
    assert levels()["deploy skew"] == "WARN"
    rows.clear()
    print("  ssh: status, bogus -> DENIED, build-id vs HEAD (dirty warns)")

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
    print("  session: idle / fresh+last_error / stale")

    # --- telemetry -------------------------------------------------------------
    doctor.check_telemetry()
    assert levels()["event stream"] == "WARN"          # nothing written in the tmp LOG_DIR
    assert levels()["log shipper"] == "PASS"
    rows.clear()

    # --- voice (filesystem + process checks only) ----------------------------------
    cglib.load_secrets = lambda: {}
    doctor.check_voice(cfg)
    names = {n for _, n, _ in rows}
    assert {"voice keys", "voice venv", "voice library", "voice agent",
            "worker CLI"} <= names, names
    assert levels()["voice keys"] == "WARN" and levels()["voice agent"] == "WARN"
    assert levels()["worker CLI"] == "PASS"
    rows.clear()
    doctor.check_voice({})
    assert levels()["voice config"] == "WARN"
    rows.clear()
    print("  voice: keys, venv, library, worker, agent rows; no voice section warns")

    print("OK - doctor: config, hardware, listener, ssh contract + deploy skew, "
          "session state, telemetry, voice rows")


if __name__ == "__main__":
    main()
