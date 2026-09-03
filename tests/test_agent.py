"""Voice_agent.main() - lane bring-up per config shape, the bench
modes, the wake loop's event order, dry-run plumbing, --once, a crashing
session. Every external seam stubbed (audio, wake model, pipeline, announcer,
operations, steam, telemetry).
"""

import json
import sys
import threading

import helpers
from helpers import fresh_state
from slopstation import cglib, events
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
    PEAK_HOPS = 0
    wakes = []  # scripted (score, capture)
    built = []

    def __init__(self, pa, voice, idx):
        FakeListener.built.append((pa, idx))

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
        self.aborted = 0
        FakeAnnouncer.made.append(self)

    def submit(self, operation):
        FakeAnnouncer.submitted.append(operation)

    def submit_notification(self, notification):
        pass

    def abort_current(self):
        self.aborted += 1


class FakeOperationStore:
    made = []

    def __init__(self, log, on_terminal=None, path=None):
        self.on_terminal = on_terminal
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
        self.store, self.steam = store, steam
        self.poll_s = 30
        self.started = False
        FakeSteamMonitor.made.append(self)

    def start(self):
        self.started = True


class FakeMediaMonitor:
    KINDS = {"movie_acquisition", "series_acquisition"}
    made = []

    def __init__(self, store, service, log, poll_s=30):
        self.store, self.service = store, service
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

    def __init__(self, secrets, log, machine_name=None):
        self.steamid = "7656"

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


class CtxLog(cglib.CapturingLog):
    """CapturingLog plus the ambient session id at each call."""

    def _write(self, level, event, fields):
        super()._write(level, event, fields)
        self.records[-1]["_session"] = events.current().get("session")


def stub_everything():
    va.open_audio = lambda voice: ("PA", 0, 1)
    va.rebuild_audio = lambda pa, voice, listener: ("PA2", 0, 1)
    va.WakeListener = FakeListener
    va.played = []
    va.play_pcm = lambda pa, pcm, idx=None: va.played.append(len(pcm))
    va.refresh_library_bg = lambda: None
    va.prewarm_imports_bg = lambda provider: None
    va.GrammarMatcher = lambda voice: "MATCHER"
    va.TvDucker = FakeDucker
    events.start_heartbeat = lambda lane, **kw: None
    cglib.rotate_log = lambda: None
    cglib.load_secrets = lambda: dict(SECRETS)
    sentry.setup = lambda cfg, log: False
    announce.Announcer = FakeAnnouncer
    operations.OperationStore = FakeOperationStore
    operations_monitors.SteamMonitor = FakeSteamMonitor
    operations_monitors.MediaMonitor = FakeMediaMonitor
    media.from_config = lambda cfg, secrets, log: (
        "MEDIA" if cfg.get("media", {}).get("enabled") else None
    )
    media.proton_port_monitor_from_config = lambda cfg, secrets, log: (
        FakeProtonPortMonitor(cfg["media"].get("pollS", 30))
        if cfg.get("media", {}).get("enabled")
        and cfg.get("media", {}).get("protonPortSync")
        else None
    )
    steam_session.SteamSession = FakeSteam
    FakeListener.wakes = []
    FakeListener.built = []
    FakeAnnouncer.made = []
    FakeAnnouncer.submitted = []
    FakeOperationStore.made = []
    FakeSteamMonitor.made = []
    FakeMediaMonitor.made = []
    FakeProtonPortMonitor.made = []
    FakeDucker.made = []
    FakeSteam.available_answer = False


def config(**top):
    cfg = json.loads(json.dumps(helpers.CONFIG))
    cfg["tvIp"] = None
    cfg["voice"]["duckSteps"] = 0
    cfg["voice"]["duckToPct"] = 0
    cfg.update(top)
    return cfg


def run(argv, cfg, session=None, setup=None):
    """main() with the given argv after stub_everything() and `setup`;
    returns (exit code or "ended", log, run_session calls)."""
    stub_everything()
    if setup:
        setup()
    cglib.use_config(cfg)
    log = CtxLog("voice")
    va.log = log
    calls = []

    async def fake_session(
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
        if session is not None:
            session()

    va.run_session = fake_session
    sys.argv = ["voice_agent.py"] + argv
    try:
        rc = va.main()
    except EndOfTest:
        rc = "ended"
    return rc, log, calls


def test_agent():
    # --- config invalid --------------------------------------------------------
    cfg = config()
    del cfg["voice"]["wakeThreshold"]
    rc, log, _ = run(["--once"], cfg)
    assert rc == 1 and log.find("config_invalid")[0]["missing"] == ["wakeThreshold"]

    # --- bench mode: --earcons ---------------------------------------------------
    rc, log, calls = run(["--earcons"], config())
    assert rc == 0 and "earcon_audition" in log.events()
    assert len(log.find("earcon_play")) == 6 and not calls

    # --- full lanes, one dry-run session -----------------------------------------
    fresh_state()
    cfg = config(tvIp="10.0.0.9")
    cfg["voice"]["duckSteps"] = 4
    rc, log, calls = run(
        ["--once", "--dry-run"],
        cfg,
        setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())),
    )
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

    # --- no Deepgram key: no session, fail earcon, capture released ----------------
    cap = FakeCapture()

    def no_keys():
        FakeListener.wakes.append((0.7, cap))
        cglib.load_secrets = lambda: {}

    rc, log, calls = run(["--once"], config(), setup=no_keys)
    assert rc == "ended" and not calls
    assert any(r["what"] == "stt" for r in log.find("lane_disabled"))
    assert "session_open" not in log.events() and cap.stopped == 1

    # --- Steam online: monitor starts and reaches the session -----------------
    def steam_online():
        FakeSteam.available_answer = True
        FakeListener.wakes.append((0.7, FakeCapture()))

    rc, log, calls = run(["--once"], config(), setup=steam_online)
    assert FakeSteamMonitor.made and FakeSteamMonitor.made[0].started
    assert any(r["what"] == "operation_monitor" for r in log.find("lane_up"))
    assert calls[0]["steam"] is FakeSteamMonitor.made[0].steam

    rc, log, calls = run(["--once", "--dry-run"], config(), setup=steam_online)
    assert not FakeSteamMonitor.made, "dry run must not observe live Steam operations"
    assert not FakeAnnouncer.made, "dry run must not deliver operation events"
    assert isinstance(calls[0]["steam"], FakeSteam)

    # --- media configured: independent monitor + tools reach the session -----
    cfg = config()
    cfg["media"]["enabled"] = True
    cfg["media"]["pollS"] = 17
    cfg["media"]["protonPortSync"] = True
    rc, log, calls = run(
        ["--once"], cfg, setup=lambda: FakeListener.wakes.append((0.7, FakeCapture()))
    )
    assert FakeMediaMonitor.made and FakeMediaMonitor.made[0].started
    assert FakeMediaMonitor.made[0].poll_s == 17
    assert calls[0]["media"] == "MEDIA"
    assert any(r["what"] == "media_operation_monitor" for r in log.find("lane_up"))
    assert FakeProtonPortMonitor.made and FakeProtonPortMonitor.made[0].started
    assert FakeProtonPortMonitor.made[0].poll_s == 17
    assert any(r["what"] == "proton_port_sync" for r in log.find("lane_up"))
    FakeProtonPortMonitor.made.clear()
    FakeMediaMonitor.made.clear()
    run(
        ["--once", "--dry-run"],
        cfg,
        setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())),
    )
    assert not any(m.started for m in FakeProtonPortMonitor.made), "vpn port"
    assert not any(m.started for m in FakeMediaMonitor.made), "authority search"

    # --- ducking without tvIp -----------------------------------------------------
    cfg = config()
    cfg["voice"]["duckSteps"] = 6
    rc, log, calls = run(
        ["--once"], cfg, setup=lambda: FakeListener.wakes.append((0.7, FakeCapture()))
    )
    assert any(r["setting"] == "duckSteps" for r in log.find("config_suspect"))
    assert not FakeDucker.made

    # --- audio opens LAST -----------------------------------------------------
    # open_audio blocks until the configured device answers - forever on a
    # dead mic - so the monitors must already exist when it is called, or a
    # microphone failure takes the whole control plane down with it.
    at_audio = {}

    def audio_last():
        FakeSteam.available_answer = True
        FakeListener.wakes.append((0.7, FakeCapture()))

        def counting_open(voice):
            at_audio["monitors"] = len(FakeSteamMonitor.made) + len(
                FakeMediaMonitor.made
            )
            return ("PA", 0, 1)

        va.open_audio = counting_open

    cfg = config()
    cfg["media"] = {"enabled": True}
    rc, log, calls = run(["--once"], cfg, setup=audio_last)
    assert at_audio["monitors"] == 2, (
        f"open_audio ran with {at_audio['monitors']} monitor(s) built - "
        "the control plane must be up before the mic wait"
    )

    # --- a crashing session -------------------------------------------------------
    def boom():
        raise RuntimeError("pipeline died")

    rc, log, calls = run(
        ["--once"],
        config(),
        session=boom,
        setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())),
    )
    assert rc == 0
    assert "session_crashed" in log.events()
    assert log.find("session_close")[0]["ending"] == "fail"
