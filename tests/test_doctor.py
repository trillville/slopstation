"""Test diagnostic checks with hardware and service probes stubbed."""

import json
import subprocess
import time
import types

import hid
import pytest
import serial

import helpers
from slopstation import config, doctor, gamepc, paths, sessionlock, statefile, supervise
from slopstation.agent.tools import media_clients, media_proton


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
class _RouteSocket:
    """A UDP socket whose getsockname names the interface reaching the PC."""

    def __init__(self, addr):
        self.addr = addr

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self, dest):
        pass

    def getsockname(self):
        return (self.addr, 0)


def test_wake_on_lan_passes_when_the_pc_network_is_among_the_senders(
    rows, cfg, monkeypatch
):
    from slopstation import couch

    monkeypatch.setattr(
        couch, "broadcast_sources", lambda: ["10.2.0.2", "192.168.68.75"]
    )
    monkeypatch.setattr(
        doctor.socket, "socket", lambda *a: _RouteSocket("192.168.68.75")
    )
    doctor.check_wol(cfg)
    assert rows.levels()["wake-on-lan"] == "PASS", rows.levels()


def test_wake_on_lan_fails_without_a_gaming_pc_ip(rows, cfg):
    """A config missing the key must not crash the rows that follow."""
    cfg.pop("gamingPcIp")
    doctor.check_wol(cfg)
    assert rows.levels()["wake-on-lan"] == "FAIL", rows.levels()


def test_wake_on_lan_fails_when_no_sender_sits_on_the_pc_network(
    rows, cfg, monkeypatch
):
    """The 2026-09-06 outage: every send left down a VPN or WSL adapter."""
    from slopstation import couch

    monkeypatch.setattr(couch, "broadcast_sources", lambda: ["10.2.0.2"])
    monkeypatch.setattr(
        doctor.socket, "socket", lambda *a: _RouteSocket("192.168.68.75")
    )
    doctor.check_wol(cfg)
    assert rows.levels()["wake-on-lan"] == "FAIL", rows.levels()


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


def _write_events(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_wake_word_readiness_is_the_latest_voice_event(rows, monkeypatch):
    """The task running proves the process; the events say whether the mic
    ever answered. Newest event wins across files, so a lane that has been
    waiting on a device since yesterday still reads as waiting."""
    from slopstation import events

    monkeypatch.setattr(supervise, "query", lambda lane: {"Status": "Running"})
    # audio_device is the input answering; the output can still miss after it.
    _write_events(
        events._path("20260913"),
        {"lane": "voice", "event": "audio_device_wait"},
        {"lane": "voice", "event": "audio_device", "kind": "input"},
    )
    doctor.check_voice_agent()
    assert rows.levels()["voice agent"] == "PASS"
    assert rows.levels()["wake word"] == "WARN"
    assert rows.detail("wake word") == "waiting for the microphone"

    rows.clear()
    _write_events(events._path("20260914"), {"lane": "voice", "event": "audio_ready"})
    doctor.check_voice_agent()
    assert rows.levels()["wake word"] == "PASS"

    # A lane that is not running gets no readiness row at all.
    rows.clear()
    monkeypatch.setattr(supervise, "query", lambda lane: {"Status": "Ready"})
    doctor.check_voice_agent()
    assert rows.levels()["voice agent"] == "WARN" and "wake word" not in rows.names()


def test_text_interface_row_reads_health_from_the_running_lane(rows, cfg, monkeypatch):
    """A real text server on a free port answers /health; the row names what
    the voice process has up and which monitors have stopped."""
    from slopstation.agent.interfaces import text

    token = "t" * 64
    monkeypatch.setattr(config, "secrets", lambda: {"textInterfaceToken": token})
    live = {**cfg, "textInterface": {"enabled": True, "host": "127.0.0.1", "port": 0}}
    server = text.start(
        live,
        {"textInterfaceToken": token, "anthropicApiKey": "a" * 64},
        helpers.CapturingLog("voice"),
        health=lambda: {
            "operations": True,
            "steam": False,
            "monitors": {"disk": False},
        },
    )
    assert server is not None
    try:
        live["textInterface"]["port"] = server.server_address[1]
        doctor.check_text(live)
        assert rows.levels()["text interface"] == "WARN"
        assert rows.detail("text interface").endswith("stopped: disk")
        assert "up: operations;" in rows.detail("text interface")
    finally:
        server.shutdown()
        server.server_close()
    rows.clear()
    doctor.check_text(live)  # nothing listening now
    assert rows.levels()["text interface"] == "WARN"
    assert rows.detail("text interface").startswith("no answer on")
    rows.clear()
    doctor.check_text({**cfg, "textInterface": {"enabled": False}})
    assert rows.levels()["text interface"] == "PASS"


def test_cron_checkin_reads_back_past_today(rows, monkeypatch):
    """A lane logs its first check-in and then only changes, so lanes that
    started days ago leave nothing in today's file and still count."""
    from slopstation import events

    monkeypatch.setattr(
        config, "load", lambda: {"sentryDsn": "https://key@o1.ingest.sentry.io/42"}
    )

    def write(path, *records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )

    archived = paths.logs() / events.ARCHIVE_NAME / events._path("20260901").name
    write(
        archived,
        {"lane": "listener", "event": "checkin"},
        {"lane": "voice", "event": "checkin_failed"},
    )
    write(events._path("20260913"), {"lane": "voice", "event": "checkin"})
    write(events._path(time.strftime("%Y%m%d")), {"lane": "voice", "event": "wake"})
    doctor.check_sentry()
    assert rows.levels()["cron check-in"] == "PASS"
    assert rows.detail("cron check-in") == "accepted for listener, voice"


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


def test_port_reservations_warn_while_the_dynamic_range_reaches_proton(
    rows, media_cfg, media_up, monkeypatch
):
    """Windows reserves ports only inside its dynamic range, so the row
    passes once that range ends below Proton's forwarded ports."""
    media_cfg["media"]["protonPortSync"] = True
    start = {"value": 58000, "count": 7536}

    def netsh(*args):
        return (
            f"Protocol {args[-1]} Dynamic Port Range\n"
            "---------------------------------\n"
            f"Start Port      : {start['value']}\n"
            f"Number of Ports : {start['count']}\n"
        )

    monkeypatch.setattr(media_proton, "_netsh", netsh)
    doctor.check_media(media_cfg)
    assert rows.levels()["port reservations"] == "WARN"
    assert "UDP 58000-65535" in rows.detail("port reservations")
    rows.clear()
    start.update(value=21000, count=11000)
    doctor.check_media(media_cfg)
    assert rows.levels()["port reservations"] == "PASS"


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
        media_clients.ArrClient,
        "get",
        lambda self, endpoint, params=None: _wanted_missing(),
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
