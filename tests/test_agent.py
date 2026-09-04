"""Test voice-agent startup, modes, event order, and error handling."""

import json
import sys
import threading
import time

import pytest

import helpers
from helpers import CapturingLog
from slopstation import config, events, logbook
from slopstation.agent import voice_agent as va
from slopstation.agent.speech import announce
from slopstation.agent.telemetry import sentry
from slopstation.agent.tools import (
    media,
    operations,
    operations_monitors,
    steam_session,
)

REAL_KEY = "k" * 40
SECRETS = {"deepgramApiKey": REAL_KEY, "anthropicApiKey": REAL_KEY}


class EndOfTest(BaseException):
    """Raised by the fake listener once its scripted wakes are spent."""


class FakeCapture:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1
        return b""


class FakeListener:
    model_name = "hey_jarvis_v0.1"
    model_source = "pretrained"
    wakes = []  # scripted (score, capture)

    def __init__(self, pa, voice, idx):
        pass

    def wait_for_wake_capture(self, threshold, on_quiet=None, interrupt=None):
        if FakeListener.wakes:
            return FakeListener.wakes.pop(0)
        raise EndOfTest()


class FakeAnnouncer:
    made = []
    submitted = []

    def __init__(self, voice, secrets, log):
        self.store = None
        self.follow_up = threading.Event()
        self.session_active = threading.Event()
        FakeAnnouncer.made.append(self)

    def submit(self, operation):
        FakeAnnouncer.submitted.append(operation)

    def submit_notification(self, notification):
        pass

    def abort_current(self):
        pass


class FakeOperationStore:
    made = []

    def __init__(self, log):
        self.on_terminal = None
        self.on_notification = None
        FakeOperationStore.made.append(self)

    def pending_announcements(self):
        return [{"id": "op-pending"}]

    def pending_notifications(self):
        return []

    def active(self, kind=None):
        return []


class FakeSteamMonitor:
    made = []

    def __init__(self, store, steam, log):
        self.steam = steam
        self.poll_s = 30
        self.started = False
        FakeSteamMonitor.made.append(self)

    def start(self):
        self.started = True


class FakeMediaMonitor:
    KINDS = {"movie_acquisition", "series_acquisition"}
    made = []

    def __init__(self, store, service, log, poll_s=30):
        self.poll_s = poll_s
        self.started = False
        FakeMediaMonitor.made.append(self)

    def start(self):
        self.started = True


class FakeProtonPortMonitor:
    made = []

    def __init__(self, poll_s=30):
        self.poll_s = poll_s
        self.started = False
        FakeProtonPortMonitor.made.append(self)

    def start(self):
        self.started = True


class FakeSteam:
    available_answer = False
    steamid = "7656"

    def __init__(self, secrets, log, machine_name=None):
        pass

    def available(self):
        return self.available_answer

    def token_expiry(self):
        return 2_000_000_000


class FakeDucker:
    made = []

    def __init__(self, steps, tv_ip, log, dry_run=False, to_pct=None):
        self.dry_run = dry_run
        FakeDucker.made.append(self)

    def duck(self):
        pass

    def unduck(self):
        pass


class CtxLog(CapturingLog):
    """CapturingLog plus the ambient session id at each call."""

    def _write(self, level, event, fields):
        super()._write(level, event, fields)
        self.records[-1]["_session"] = events.current().get("session")


def make_config(**top):
    cfg = json.loads(json.dumps(helpers.CONFIG))
    cfg["tvIp"] = None
    cfg["voice"]["duckSteps"] = 0
    cfg["voice"]["duckToPct"] = 0
    cfg.update(top)
    return cfg


def media_config():
    """Media on, with a poll interval the monitors must pick up."""
    cfg = make_config()
    cfg["media"]["enabled"] = True
    cfg["media"]["pollS"] = 17
    cfg["media"]["protonPortSync"] = True
    return cfg


def one_wake():
    """The listener's script: a single wake, then EndOfTest."""
    return [(0.7, FakeCapture())]


@pytest.fixture
def stubbed(monkeypatch):
    """Replace external calls and reset fake recorders."""
    for cls in (
        FakeAnnouncer,
        FakeOperationStore,
        FakeSteamMonitor,
        FakeMediaMonitor,
        FakeProtonPortMonitor,
        FakeDucker,
    ):
        monkeypatch.setattr(cls, "made", [])
    monkeypatch.setattr(FakeListener, "wakes", [])
    monkeypatch.setattr(FakeAnnouncer, "submitted", [])
    monkeypatch.setattr(FakeSteam, "available_answer", False)
    monkeypatch.setattr(va, "open_audio", lambda voice: ("PA", 0, 1))
    monkeypatch.setattr(va, "rebuild_audio", lambda pa, voice, listener: ("PA2", 0, 1))
    monkeypatch.setattr(va, "WakeListener", FakeListener)
    monkeypatch.setattr(va, "play_pcm", lambda pa, pcm, idx=None: None)
    monkeypatch.setattr(va, "refresh_library_bg", lambda: None)
    monkeypatch.setattr(va, "prewarm_imports_bg", lambda provider: None)
    monkeypatch.setattr(va, "GrammarMatcher", lambda voice: "MATCHER")
    monkeypatch.setattr(va, "TvDucker", FakeDucker)
    monkeypatch.setattr(events, "start_heartbeat", lambda lane, **kw: None)
    monkeypatch.setattr(logbook, "rotate", lambda: None)
    monkeypatch.setattr(config, "secrets", lambda: dict(SECRETS))
    monkeypatch.setattr(sentry, "setup", lambda cfg, log: False)
    monkeypatch.setattr(announce, "Announcer", FakeAnnouncer)
    monkeypatch.setattr(operations, "OperationStore", FakeOperationStore)
    monkeypatch.setattr(operations_monitors, "SteamMonitor", FakeSteamMonitor)
    monkeypatch.setattr(operations_monitors, "MediaMonitor", FakeMediaMonitor)
    monkeypatch.setattr(
        media,
        "from_config",
        lambda cfg, secrets, log: (
            "MEDIA" if cfg.get("media", {}).get("enabled") else None
        ),
    )
    monkeypatch.setattr(
        media,
        "proton_port_monitor_from_config",
        lambda cfg, secrets, log: (
            FakeProtonPortMonitor(cfg["media"].get("pollS", 30))
            if cfg.get("media", {}).get("enabled")
            and cfg.get("media", {}).get("protonPortSync")
            else None
        ),
    )
    monkeypatch.setattr(steam_session, "SteamSession", FakeSteam)


@pytest.fixture
def run(monkeypatch, stubbed):
    """main() with the given argv over the stubs; returns (exit code or
    "ended", log, the Sessions built). `wakes` scripts the listener and
    `session` runs inside the faked Session.run."""

    def run(argv, cfg, wakes=(), session=None):
        FakeListener.wakes.extend(wakes)
        monkeypatch.setattr(config, "_current", cfg)
        log = CtxLog("voice")
        monkeypatch.setattr(va, "log", log)
        calls = []

        class FakeSession:
            def __init__(
                self,
                cfg,
                secrets,
                matcher,
                dry_run,
                input_idx,
                output_idx,
                capture=None,
                operations=None,
                ack=None,
                steam=None,
                media=None,
                on_end_session=None,
            ):
                calls.append(
                    dict(
                        dry_run=dry_run,
                        operations=operations,
                        steam=steam,
                        media=media,
                        capture=capture,
                        matcher=matcher,
                        on_end_session=on_end_session,
                    )
                )

            async def run(self):
                if session is not None:
                    session()

        monkeypatch.setattr(va, "Session", FakeSession)
        monkeypatch.setattr(sys, "argv", ["voice_agent.py"] + argv)
        try:
            rc = va.main()
        except EndOfTest:
            rc = "ended"
        return rc, log, calls

    return run


def test_a_missing_voice_key_fails_startup(run):
    cfg = make_config()
    del cfg["voice"]["wakeThreshold"]
    rc, log, _ = run(["--once"], cfg)
    assert rc == 1 and log.find("config_invalid")[0]["missing"] == ["wakeThreshold"]


def test_earcons_bench_plays_the_vocabulary_and_exits(run, monkeypatch):
    monkeypatch.setattr(
        time, "sleep", lambda s: None
    )  # the bench paces each earcon by 0.7 s
    rc, log, calls = run(["--earcons"], make_config())
    assert rc == 0 and "earcon_audition" in log.events()
    assert len(log.find("earcon_play")) == 6 and not calls


def test_full_lanes_run_one_dry_session(run):
    cfg = make_config(tvIp="10.0.0.9")
    cfg["voice"]["duckSteps"] = 4
    rc, log, calls = run(["--once", "--dry-run"], cfg, wakes=one_wake())
    assert rc == 0, rc
    ev = log.events()
    ups = {r["what"] for r in log.find("lane_up")}
    assert "assistant" in ups, ups
    assert any(r["what"] == "steam_session" for r in log.find("lane_disabled"))
    assert log.find("agent_up")[0]["dry_run"] is True
    # the loop: wake, then the session, then the sleep chime + close
    assert ev.index("wake") < ev.index("session_open") < ev.index("session_close")
    assert log.find("wake")[0]["trigger"] == "wake_word"
    assert log.find("session_close")[0]["ending"] == "close"
    # the wake carries the session it opens (events.context is merged per wake)
    wake, opened = log.find("wake")[0], log.find("session_open")[0]
    assert wake["_session"] and wake["_session"] == opened["_session"]
    # dry_run reaches the room side effects and the session
    assert calls and calls[0]["dry_run"] is True
    assert FakeDucker.made and FakeDucker.made[0].dry_run is True
    store = FakeOperationStore.made[0]
    assert not FakeAnnouncer.made, "a dry run must not construct a live announcer"
    assert store.on_terminal is None and store.on_notification is None
    assert calls[0]["operations"] is store and calls[0]["matcher"] == "MATCHER"
    assert calls[0]["capture"].stopped >= 1, "capture must be stopped after the session"
    # end_session restores the room while the TV is still on (dispatch calls it)
    assert callable(calls[0]["on_end_session"])


def test_no_deepgram_key_opens_no_session_and_releases_the_capture(monkeypatch, run):
    cap = FakeCapture()
    monkeypatch.setattr(config, "secrets", lambda: {})
    rc, log, calls = run(["--once"], make_config(), wakes=[(0.7, cap)])
    assert rc == "ended" and not calls
    assert any(r["what"] == "stt" for r in log.find("lane_disabled"))
    assert "session_open" not in log.events() and cap.stopped == 1


def test_steam_online_starts_the_monitor_and_reaches_the_session(monkeypatch, run):
    monkeypatch.setattr(FakeSteam, "available_answer", True)
    rc, log, calls = run(["--once"], make_config(), wakes=one_wake())
    assert FakeSteamMonitor.made and FakeSteamMonitor.made[0].started
    assert any(r["what"] == "operation_monitor" for r in log.find("lane_up"))
    assert calls[0]["steam"] is FakeSteamMonitor.made[0].steam
    # A live run replays what was pending at boot to the announcer.
    assert FakeAnnouncer.submitted == [{"id": "op-pending"}]


def test_a_dry_run_keeps_live_steam_out_of_the_operation_lanes(monkeypatch, run):
    monkeypatch.setattr(FakeSteam, "available_answer", True)
    rc, log, calls = run(["--once", "--dry-run"], make_config(), wakes=one_wake())
    assert not FakeSteamMonitor.made, "dry run must not observe live Steam operations"
    assert isinstance(calls[0]["steam"], FakeSteam)


def test_media_configured_starts_its_monitors_and_reaches_the_session(run):
    rc, log, calls = run(["--once"], media_config(), wakes=one_wake())
    assert FakeMediaMonitor.made and FakeMediaMonitor.made[0].started
    assert FakeMediaMonitor.made[0].poll_s == 17
    assert calls[0]["media"] == "MEDIA"
    assert any(r["what"] == "media_operation_monitor" for r in log.find("lane_up"))
    assert FakeProtonPortMonitor.made and FakeProtonPortMonitor.made[0].started
    assert FakeProtonPortMonitor.made[0].poll_s == 17
    assert any(r["what"] == "proton_port_sync" for r in log.find("lane_up"))


def test_a_dry_run_starts_no_media_monitor(run):
    run(["--once", "--dry-run"], media_config(), wakes=one_wake())
    assert not any(m.started for m in FakeProtonPortMonitor.made), "vpn port"
    assert not any(m.started for m in FakeMediaMonitor.made), "authority search"


def test_ducking_without_a_tv_ip_stays_off(run):
    cfg = make_config()
    cfg["voice"]["duckSteps"] = 6
    rc, log, calls = run(["--once"], cfg, wakes=one_wake())
    assert any(r["setting"] == "duckSteps" for r in log.find("config_suspect"))
    assert not FakeDucker.made


def test_audio_opens_last(monkeypatch, run):
    # open_audio blocks until the configured device answers - forever on a
    # dead mic - so the monitors must already exist when it is called, or a
    # microphone failure takes the whole control plane down with it.
    at_audio = {}
    monkeypatch.setattr(FakeSteam, "available_answer", True)

    def counting_open(voice):
        at_audio["monitors"] = len(FakeSteamMonitor.made) + len(FakeMediaMonitor.made)
        return ("PA", 0, 1)

    monkeypatch.setattr(va, "open_audio", counting_open)
    cfg = make_config()
    cfg["media"] = {"enabled": True}
    rc, log, calls = run(["--once"], cfg, wakes=one_wake())
    assert at_audio["monitors"] == 2, (
        f"open_audio ran with {at_audio['monitors']} monitor(s) built - "
        "the control plane must be up before the mic wait"
    )


def test_a_crashing_session_closes_with_fail(run):
    def boom():
        raise RuntimeError("pipeline died")

    rc, log, calls = run(["--once"], make_config(), wakes=one_wake(), session=boom)
    assert rc == 0
    assert "session_crashed" in log.events()
    assert log.find("session_close")[0]["ending"] == "fail"
