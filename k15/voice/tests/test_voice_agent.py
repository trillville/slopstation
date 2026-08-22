"""Blind test: the voice agent's bring-up decisions - the composition root
that had no test at all. Each optional lane decides for itself and says so
with a lane_up / lane_disabled / config_suspect event; that is what the
couch reads when something is off, so it is what gets pinned: the ducker's
config gates, the account session's token gate, and the worker lane's three
refusals. No audio, no network, no CLI. Run:
    .venv\\Scripts\\python tests\\test_voice_agent.py
"""
import _bootstrap  # noqa: F401
import base64
import json
import time

import cglib
import ducking
import voice_agent as va
import workers


def jwt(exp):
    p = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{p}.sig"


def main():
    # --- build_ducker: off by default, off without tvIp, pct sanity ----------
    log = cglib.CapturingLog("voice")
    assert va.build_ducker({}, {}, False, log=log) is None and log.events() == []

    log = cglib.CapturingLog("voice")
    assert va.build_ducker({}, {"duckSteps": 10}, False, log=log) is None
    w = log.find("config_suspect")
    assert w and w[0]["setting"] == "duckSteps", log.records   # configured, no tvIp

    log = cglib.CapturingLog("voice")
    assert va.build_ducker({"tvIp": "192.0.2.1"}, {"duckToPct": 150}, False, log=log) is None
    w = log.find("config_suspect")
    assert w and w[0]["setting"] == "duckToPct", log.records   # out of range -> ignored

    log = cglib.CapturingLog("voice")
    dk = va.build_ducker({"tvIp": "192.0.2.1"}, {"duckSteps": 5, "duckToPct": 50},
                         True, log=log)
    assert isinstance(dk, ducking.TvDucker) and dk.steps == 5 and dk.to_pct == 50
    assert dk.dry_run is True and log.events() == []
    print("  build_ducker: off/no-tvIp/bad-pct say config_suspect; configured builds")

    # --- steam_lane: no token -> None and lane_disabled; token -> lane_up ----
    log = cglib.CapturingLog("voice")
    assert va.steam_lane({}, {}, log=log) is None
    d = log.find("lane_disabled")
    assert d and d[0]["what"] == "steam_session", log.records
    log = cglib.CapturingLog("voice")
    s = va.steam_lane({"steamMachineName": "PC"},
                      {"steamId64": "76561190000",
                       "steamRefreshToken": jwt(time.time() + 200 * 86400)}, log=log)
    assert s is not None and s.machine == "PC"
    up = log.find("lane_up")
    assert up and up[0]["what"] == "steam_session" and up[0]["token_expires"], log.records
    print("  steam_lane: token gate says lane_disabled / lane_up")

    # --- worker_lane: the three refusals, each with its reason ---------------
    voice = {"workerProvider": "nope", "workerModelAnthropic": "", "workerEffort": "",
             "workerTimeoutS": 1}
    log = cglib.CapturingLog("voice")
    assert va.worker_lane(voice, {}, True, True, False, log=log) == (None, None)
    d = log.find("lane_disabled")
    assert d and d[0]["reason"] == "unknown workerProvider", log.records

    real_which = workers.shutil.which
    try:
        workers.shutil.which = lambda exe: None                 # CLI absent
        voice["workerProvider"] = "anthropic"
        log = cglib.CapturingLog("voice")
        assert va.worker_lane(voice, {}, True, True, False, log=log) == (None, None)
        assert log.find("lane_disabled")[0]["reason"] == "CLI not on PATH", log.records

        workers.shutil.which = lambda exe: r"C:\x\claude.exe"  # present...
        log = cglib.CapturingLog("voice")
        assert va.worker_lane(voice, {}, False, True, False, log=log) == (None, None)
        assert "Deepgram" in log.find("lane_disabled")[0]["reason"]   # ...but no STT
    finally:
        workers.shutil.which = real_which
    print("  worker_lane: unknown provider / no CLI / no keys each refuse and say why")

    print("OK - voice_agent: ducker, steam and worker lanes decide and say so")


if __name__ == "__main__":
    main()
