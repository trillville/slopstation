"""Blind test: dispatch.py logic with every side effect mocked -
lock arbiter, dry-run, volume stepping + clamp, mute, input map + the
READY-gate on the gaming input, serial retry, ssh outcomes. Run:
    .venv\\Scripts\\python tests\\test_dispatch.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cglib
import couch                 # THE ssh seam - dispatch reaches it through the module
import dispatch as dp

CFG = {
    "tvComPort": "COMX", "tvGamingCmd": "hdmi4",
    "voice": {
        "volumeStep": 3, "volumeMax": 40,
        "inputs": {"apple tv": "hdmi1", "the pc": "hdmi4"},
    },
}


class Harness:
    def __init__(self, dry_run=False):
        self.log = cglib.CapturingLog("dispatch")
        self.d = dp.Dispatch(CFG, self.log, dry_run=dry_run)


def with_temp_lock(age_s):
    """Point cglib.LOCK at a temp file with the given age; None = absent."""
    tmp = Path(tempfile.mkdtemp()) / "session.lock"
    if age_s is not None:
        tmp.write_text("x")
        old = time.time() - age_s
        import os
        os.utime(tmp, (old, old))
    cglib.LOCK = tmp


def main():
    sent = []
    real_sleep = time.sleep
    time.sleep = lambda s: None                       # fast tests
    cglib.exlink_send_hex = lambda frame, port: sent.append(frame) or "030cf1"

    # --- lock arbiter --------------------------------------------------------
    with_temp_lock(10)                                # fresh lock
    h = Harness(dry_run=True)
    r = h.d.start_session()
    assert not r.ok and r.earcon == "busy", r

    with_temp_lock(None)                              # no lock
    h = Harness(dry_run=True)
    r = h.d.start_session(appid=12345)
    assert r.ok and "couch.py start 12345" in r.detail, r
    # Assert on the EVENT, not its prose: rewording is free, renaming is not
    # (alerts and dashboards group by event name).
    assert "dry_run_would" in h.log.events(), h.log.records

    with_temp_lock(999)                               # stale lock = launchable
    h = Harness(dry_run=True)
    assert h.d.start_session().ok

    # --- live start spawns couch.py ------------------------------------------
    with_temp_lock(None)
    spawned = []
    dp.subprocess.Popen = lambda args, **kw: spawned.append(args)
    h = Harness()
    r = h.d.start_session(appid=777)
    # Positional, not tail-anchored: a turn id may follow (E1), and this
    # assertion is about the verb and the appid.
    i = spawned[0].index("start")
    assert r.ok and spawned[0][i:i + 2] == ["start", "777"], spawned

    # --- volume: stepping, clamp, mute ---------------------------------------
    h = Harness(); sent.clear()
    assert h.d.volume_up().ok
    assert sent == [cglib.EXLINK_FRAMES["vol_up"]] * 3, sent   # volumeStep=3

    sent.clear()
    r = h.d.volume_set(80)                            # clamps to volumeMax 40
    assert r.ok and sent == [cglib.vol_set_frame(40)], sent
    sent.clear()
    assert h.d.volume_set(25).ok and sent == [cglib.vol_set_frame(25)]

    sent.clear()
    assert h.d.mute_toggle().ok and sent == [cglib.EXLINK_FRAMES["mute_toggle"]]

    # --- serial send raises -> fail earcon (COM retry now lives in cglib) ----
    def always_down(frame, port): raise OSError("dead")
    cglib.exlink_send_hex = always_down
    r = h.d.mute_toggle()
    assert not r.ok and r.earcon == "fail"

    # --- input map + gaming-input semantics ----------------------------------
    cglib.exlink_send_hex = lambda frame, port: sent.append(frame) or "030cf1"
    h = Harness(); sent.clear()
    assert not h.d.switch_input("garage").ok          # unknown name
    assert h.d.switch_input("Apple TV ").ok           # case/space tolerant
    assert sent == [cglib.EXLINK_FRAMES["hdmi1"]]

    # No session: "switch to the pc" = "start a session" - it
    # spawns the full couch launch, never refuses, never touches the TV
    # itself (couch.py flips the input at READY).
    with_temp_lock(None)
    sent.clear(); spawned.clear()
    r = h.d.switch_input("the pc")
    assert r.ok and spawned and spawned[0][-1] == "start" and not sent, \
        (r, spawned, sent)
    # Mid-launch (fresh lock, host pre-READY): truthful busy, no switch.
    with_temp_lock(10)
    couch.ssh = lambda cmd, **kw: "NOTREADY"
    sent.clear()
    r = h.d.switch_input("the pc")
    assert not r.ok and r.earcon == "busy" and not sent, r
    # Live READY session: flips instantly.
    couch.ssh = lambda cmd, **kw: "2026-08-10T20:00:00"  # READY timestamp
    assert h.d.switch_input("the pc").ok and sent == [cglib.EXLINK_FRAMES["hdmi4"]]
    # Fresh lock but host unreachable: honest fail, no switch.
    def ssh_down(cmd, **kw): raise RuntimeError("unreachable")
    couch.ssh = ssh_down
    sent.clear()
    assert not h.d.switch_input("the pc").ok and not sent

    # --- end session over ssh outcomes ---------------------------------------
    couch.ssh = lambda cmd, **kw: "OK"
    assert Harness().d.end_session().ok
    couch.ssh = lambda cmd, **kw: "FAILED:1"
    assert not Harness().d.end_session().ok
    couch.ssh = ssh_down
    r = Harness().d.end_session()
    assert not r.ok and r.earcon == "fail"

    # --- play_game: session-live ssh outcomes + cold-start delegation --------
    with_temp_lock(10)                                # fresh lock = session up
    h = Harness()
    couch.ssh = lambda cmd, **kw: "OK"
    assert h.d.play_game(1888160).ok
    couch.ssh = lambda cmd, **kw: "ALREADY"
    assert h.d.play_game(1).ok
    couch.ssh = lambda cmd, **kw: "BUSY:42"
    r = h.d.play_game(1)
    # The blocker is named for the assistant lane (detail is all it sees);
    # an index miss degrades to the bare id, never to a crash.
    assert not r.ok and r.earcon == "busy" and "BUSY:42" in r.detail, r
    dp.library.installed_name = lambda a: {42: "Baldur's Gate 3"}.get(a)
    r = h.d.play_game(1)
    assert "Baldur's Gate 3 is already running" in r.detail, r
    assert "controller" in r.detail, r          # and what to do about it
    dp.library.installed_name = lambda a: None
    assert "app 42 is already running" in h.d.play_game(1).detail
    couch.ssh = lambda cmd, **kw: "NOTREADY"             # launch still in flight
    r = h.d.play_game(1)
    assert not r.ok and r.earcon == "busy"
    couch.ssh = lambda cmd, **kw: "NOTINSTALLED"         # PC-side install guard
    r = h.d.play_game(1)
    assert not r.ok and r.earcon == "fail" and "not installed" in r.detail
    assert "controller" in r.detail, r
    couch.ssh = ssh_down
    assert Harness().d.play_game(1).earcon == "fail"
    with_temp_lock(None)                              # cold: full couch launch
    spawned.clear()
    r = Harness().d.play_game(777)
    i = spawned[0].index("start")
    assert r.ok and spawned[0][i:i + 2] == ["start", "777"], spawned

    time.sleep = real_sleep
    print("OK - dispatch: lock arbiter, dry-run, spawn args, volume step/clamp, "
          "mute, retry, input map, gaming-input autostart/busy/READY, "
          "ssh outcomes, play_game paths")


if __name__ == "__main__":
    main()
