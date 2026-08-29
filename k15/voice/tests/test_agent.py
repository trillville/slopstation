"""Blind test: voice_agent.main() - lane bring-up per config shape, the bench
modes, the wake loop's event order, dry-run plumbing, --once, a crashing
session. Every external seam stubbed (audio, wake model, pipeline, announcer,
operations, steam, tracing). Named test_agent, not test_voice_agent: Start-K15.bat's
reload and doctor find the live agent by the substring voice_agent.py. Run:
    .venv\\Scripts\\python tests\\test_agent.py
"""
import json
import sys
import threading

import _bootstrap  # noqa: F401
from _bootstrap import fresh_state

import announce
import cglib
import events
import media
import operations
import steam_session
import tracing
import voice_agent as va

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
    wakes = []                                   # scripted (score, capture)
    built = []

    def __init__(self, pa, voice, idx):
        FakeListener.built.append((pa, idx))

    def wait_for_wake_capture(self, threshold, on_quiet=None, interrupt=None):
        if FakeListener.wakes:
            return FakeListener.wakes.pop(0)
        raise EndOfTest()


class FakeAnnouncer:
    made = []

    def __init__(self, voice, secrets, log):
        self.store = None
        self.follow_up = threading.Event()
        self.session_active = threading.Event()
        self.aborted = 0
        FakeAnnouncer.made.append(self)

    def submit(self, job):
        pass

    def abort_current(self):
        self.aborted += 1


class FakeOperationStore:
    made = []

    def __init__(self, log, on_terminal=None, path=None):
        self.on_terminal = on_terminal
        FakeOperationStore.made.append(self)

    def pending_announcements(self):
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
    tracing.setup = lambda cfg, secrets, log: False
    announce.Announcer = FakeAnnouncer
    operations.OperationStore = FakeOperationStore
    operations.SteamMonitor = FakeSteamMonitor
    operations.MediaMonitor = FakeMediaMonitor
    media.from_config = lambda cfg, secrets, log: (
        "MEDIA" if cfg.get("media", {}).get("enabled") else None)
    steam_session.SteamSession = FakeSteam
    FakeListener.wakes = []
    FakeListener.built = []
    FakeAnnouncer.made = []
    FakeOperationStore.made = []
    FakeSteamMonitor.made = []
    FakeMediaMonitor.made = []
    FakeDucker.made = []
    FakeSteam.available_answer = False


def config(**top):
    cfg = json.loads(json.dumps(_bootstrap.CONFIG))
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

    async def fake_session(cfg, secrets, matcher, dry_run, input_idx, output_idx,
                           capture=None, operations=None, ack=None, steam=None,
                           media=None, on_end_session=None):
        calls.append(dict(dry_run=dry_run, operations=operations, steam=steam,
                          media=media, capture=capture, matcher=matcher,
                          on_end_session=on_end_session))
        if session is not None:
            session()
    va.run_session = fake_session
    sys.argv = ["voice_agent.py"] + argv
    try:
        rc = va.main()
    except EndOfTest:
        rc = "ended"
    return rc, log, calls


def main():
    # --- config invalid --------------------------------------------------------
    cfg = config()
    del cfg["voice"]["wakeThreshold"]
    rc, log, _ = run(["--once"], cfg)
    assert rc == 1 and log.find("config_invalid")[0]["missing"] == ["wakeThreshold"]
    print("  config: a missing voice key is config_invalid + exit 1")

    # --- bench mode: --earcons ---------------------------------------------------
    rc, log, calls = run(["--earcons"], config())
    assert rc == 0 and "earcon_audition" in log.events()
    assert len(log.find("earcon_play")) == 6 and not calls
    print("  bench: --earcons plays the vocabulary and exits 0")

    # --- full lanes, one dry-run session -----------------------------------------
    fresh_state()
    cfg = config(tvIp="10.0.0.9")
    cfg["voice"]["duckSteps"] = 4
    rc, log, calls = run(["--once", "--dry-run"], cfg,
                         setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())))
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
    # the announcer<->store wiring (two-phase attach)
    ann, store = FakeAnnouncer.made[0], FakeOperationStore.made[0]
    assert ann.store is store and store.on_terminal == ann.submit
    assert calls[0]["operations"] is store and calls[0]["matcher"] == "MATCHER"
    assert calls[0]["capture"].stopped >= 1, "capture must be stopped after the session"
    # end_session restores the room while the TV is still on (dispatch calls it)
    assert callable(calls[0]["on_end_session"])
    print("  lanes: assistant up, Steam off, operations+ducker built; one dry-run session")

    # --- no Deepgram key: no session, fail earcon, capture released ----------------
    cap = FakeCapture()

    def no_keys():
        FakeListener.wakes.append((0.7, cap))
        cglib.load_secrets = lambda: {}
    rc, log, calls = run(["--once"], config(), setup=no_keys)
    assert rc == "ended" and not calls
    assert any(r["what"] == "stt" for r in log.find("lane_disabled"))
    assert "session_open" not in log.events() and cap.stopped == 1
    print("  stt off: wake plays the fail earcon, opens no session, releases the mic")

    # --- Steam online: monitor starts and reaches the session -----------------
    def steam_online():
        FakeSteam.available_answer = True
        FakeListener.wakes.append((0.7, FakeCapture()))
    rc, log, calls = run(["--once"], config(), setup=steam_online)
    assert FakeSteamMonitor.made and FakeSteamMonitor.made[0].started
    assert any(r["what"] == "operation_monitor" for r in log.find("lane_up"))
    assert calls[0]["steam"] is FakeSteamMonitor.made[0].steam
    print("  operations: enrolled Steam starts the monitor and reaches the session")

    # --- media configured: independent monitor + tools reach the session -----
    cfg = config()
    cfg["media"]["enabled"] = True
    cfg["media"]["pollS"] = 17
    rc, log, calls = run(["--once"], cfg,
                         setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())))
    assert FakeMediaMonitor.made and FakeMediaMonitor.made[0].started
    assert FakeMediaMonitor.made[0].poll_s == 17
    assert calls[0]["media"] == "MEDIA"
    assert any(r["what"] == "media_operation_monitor" for r in log.find("lane_up"))
    print("  media: configured service starts its monitor and reaches the session")

    # --- ducking without tvIp -----------------------------------------------------
    cfg = config()
    cfg["voice"]["duckSteps"] = 6
    rc, log, calls = run(["--once"], cfg,
                         setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())))
    assert any(r["setting"] == "duckSteps" for r in log.find("config_suspect"))
    assert not FakeDucker.made
    print("  ducking: steps without tvIp warns and stays off")

    # --- a crashing session -------------------------------------------------------
    def boom():
        raise RuntimeError("pipeline died")
    rc, log, calls = run(["--once"], config(), session=boom,
                         setup=lambda: FakeListener.wakes.append((0.7, FakeCapture())))
    assert rc == 0
    assert "session_crashed" in log.events()
    assert log.find("session_close")[0]["ending"] == "fail"
    print("  crash: session_crashed, close ending=fail, the agent survives")

    print("OK - agent: config gate, bench mode, lanes per config, wake loop order, "
          "dry-run plumbing, --once, stt-off and crash paths")


if __name__ == "__main__":
    main()
