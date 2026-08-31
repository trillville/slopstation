"""Blind test: dispatch.py logic with every side effect mocked - lock arbiter,
dry-run, volume stepping + clamp, mute, input map + the READY-gate on the
gaming input, serial retry, ssh outcomes. Run:
    .venv\\Scripts\\python tests\\test_dispatch.py
"""
import time

import _bootstrap  # noqa: F401
from _bootstrap import fresh_state

import cglib
import tv
import gamepc                 # the ssh seam - dispatch reaches it via the module
from agent.brain import dispatch as dp

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


def main():
    sent = []
    real_sleep = time.sleep
    time.sleep = lambda s: None                       # fast tests
    tv.exlink_send_hex = lambda frame, port: sent.append(frame) or "030cf1"

    # --- lock arbiter --------------------------------------------------------
    fresh_state(10)                                # fresh lock
    h = Harness(dry_run=True)
    r = h.d.start_session()
    assert not r.ok and r.earcon == "busy", r

    fresh_state(None)
    h = Harness(dry_run=True)
    r = h.d.start_session(appid=12345)
    assert r.ok and "couch.py start 12345" in r.detail, r
    # Assert on the EVENT, not its prose: dashboards group by event name.
    assert "dry_run_would" in h.log.events(), h.log.records

    fresh_state(999)                               # stale lock = launchable
    h = Harness(dry_run=True)
    assert h.d.start_session().ok

    # --- live start spawns couch.py ------------------------------------------
    fresh_state(None)
    spawned = []
    dp.subprocess.Popen = lambda args, **kw: spawned.append(args)
    h = Harness()
    r = h.d.start_session(appid=777)
    # Positional, not tail-anchored: a turn id may follow the appid.
    i = spawned[0].index("start")
    assert r.ok and spawned[0][i:i + 2] == ["start", "777"], spawned

    # --- volume: stepping, clamp, mute ---------------------------------------
    h = Harness(); sent.clear()
    assert h.d.volume_up().ok
    assert sent == [tv.EXLINK_FRAMES["vol_up"]] * 3, sent   # volumeStep=3

    sent.clear()
    r = h.d.volume_set(80)                            # clamps to volumeMax 40
    assert r.ok and sent == [tv.vol_set_frame(40)], sent
    sent.clear()
    assert h.d.volume_set(25).ok and sent == [tv.vol_set_frame(25)]

    sent.clear()
    assert h.d.mute_toggle().ok and sent == [tv.EXLINK_FRAMES["mute_toggle"]]

    # duck/unduck left Dispatch on 2026-08-21: with eARC audio the TV refuses
    # Ex-Link volume writes, so ducking is remote-key relay + readback.

    # --- serial send raises -> fail earcon (COM retry lives in tv.exlink_send_hex) --
    def always_down(frame, port): raise OSError("dead")
    tv.exlink_send_hex = always_down
    r = h.d.mute_toggle()
    assert not r.ok and r.earcon == "fail"

    # --- input map + gaming-input semantics ----------------------------------
    tv.exlink_send_hex = lambda frame, port: sent.append(frame) or "030cf1"
    h = Harness(); sent.clear()
    assert not h.d.switch_input("garage").ok          # unknown name
    assert h.d.switch_input("Apple TV ").ok
    assert sent == [tv.EXLINK_FRAMES["hdmi1"]]

    # No session: "switch to the pc" means "start a session" - spawns the full
    # couch launch and never touches the TV (couch.py flips at READY).
    fresh_state(None)
    sent.clear(); spawned.clear()
    r = h.d.switch_input("the pc")
    assert r.ok and spawned and spawned[0][-1] == "start" and not sent, \
        (r, spawned, sent)
    # Mid-launch (fresh lock, host pre-READY): truthful busy, no switch.
    fresh_state(10)
    gamepc.ssh = lambda cmd, **kw: "NOTREADY"
    sent.clear()
    r = h.d.switch_input("the pc")
    assert not r.ok and r.earcon == "busy" and not sent, r
    gamepc.ssh = lambda cmd, **kw: "2026-08-10T20:00:00"  # READY timestamp
    assert h.d.switch_input("the pc").ok and sent == [tv.EXLINK_FRAMES["hdmi4"]]
    # Fresh lock but host unreachable: honest fail, no switch.
    def ssh_down(cmd, **kw): raise RuntimeError("unreachable")
    gamepc.ssh = ssh_down
    sent.clear()
    assert not h.d.switch_input("the pc").ok and not sent

    # --- end session over ssh outcomes ---------------------------------------
    # With the rig busy, ending writes the cancel marker couch.py consumes
    # BEFORE the ssh, so the launch stands down even if the exit never lands.
    fresh_state(10)
    gamepc.ssh = lambda cmd, **kw: "OK"
    h = Harness()
    assert h.d.end_session().ok
    assert cglib.CANCEL.exists(), "a busy rig's end must leave the marker"
    assert "end_session_dispatched" in h.log.events()
    fresh_state(10)
    gamepc.ssh = lambda cmd, **kw: "FAILED:1"
    assert not Harness().d.end_session().ok
    fresh_state(10)                    # mid-launch, PC mid-wake: still an end
    gamepc.ssh = ssh_down
    h = Harness()
    r = h.d.end_session()
    assert r.ok and cglib.CANCEL.exists(), r
    assert "end_session_dispatched" in h.log.events()
    # Idle rig: nothing to cancel, so an unreachable PC is a real failure.
    fresh_state(None)
    r = Harness().d.end_session()
    assert not r.ok and r.earcon == "fail"
    assert not cglib.CANCEL.exists(), "an idle rig's end must not leave a marker"

    # The room ducker restores HERE, before the exit: the voice session stays
    # open for the idle timeout, by which time couch has cut TV power and
    # remote keys relay nothing (2026-08-22).
    fresh_state(10)
    order = []
    gamepc.ssh = lambda cmd, **kw: order.append(f"ssh {cmd}") or "OK"
    h = Harness()
    h.d.on_end_session = lambda: order.append("restore")
    assert h.d.end_session().ok
    assert order[0] == "restore", order
    # A hook that raises is a warn, never a failed teardown.
    h = Harness()
    h.d.on_end_session = lambda: 1 / 0
    assert h.d.end_session().ok
    assert "end_session_hook_failed" in h.log.events(), h.log.records
    # Dry run restores too - the TV is real even when the ssh is not.
    hooked = []
    h = Harness(dry_run=True)
    h.d.on_end_session = lambda: hooked.append(1)
    assert h.d.end_session().ok and hooked == [1]

    # --- play_game: session-live ssh outcomes + cold-start delegation --------
    fresh_state(10)                                # fresh lock = session up
    h = Harness()
    gamepc.ssh = lambda cmd, **kw: "OK"
    assert h.d.play_game(1888160).ok
    gamepc.ssh = lambda cmd, **kw: "ALREADY"
    assert h.d.play_game(1).ok
    gamepc.ssh = lambda cmd, **kw: "BUSY:42"
    r = h.d.play_game(1)
    # The blocker is named for the assistant lane; an index miss degrades to
    # the bare id, never to a crash.
    assert not r.ok and r.earcon == "busy" and "BUSY:42" in r.detail, r
    dp.library.installed_name = lambda a: {42: "Baldur's Gate 3"}.get(a)
    r = h.d.play_game(1)
    assert "Baldur's Gate 3 is already running" in r.detail, r
    assert "quit" in r.detail, r                 # the BUSY message now OFFERS the quit
    dp.library.installed_name = lambda a: None
    assert "app 42 is already running" in h.d.play_game(1).detail
    gamepc.ssh = lambda cmd, **kw: "NOTREADY"             # launch still in flight
    r = h.d.play_game(1)
    assert not r.ok and r.earcon == "busy"
    gamepc.ssh = lambda cmd, **kw: "NOTINSTALLED"         # PC-side install guard
    r = h.d.play_game(1)
    assert not r.ok and r.earcon == "fail" and "not installed" in r.detail
    assert "controller" in r.detail, r
    gamepc.ssh = ssh_down
    assert Harness().d.play_game(1).earcon == "fail"
    fresh_state(None)                              # cold: full couch launch
    spawned.clear()
    r = Harness().d.play_game(777)
    i = spawned[0].index("start")
    assert r.ok and spawned[0][i:i + 2] == ["start", "777"], spawned

    # --- quit_game: correlated wire command, ssh outcomes, wrong-game refusal -
    h = Harness()
    wire = []
    gamepc.ssh = lambda cmd, **kw: wire.append(cmd) or "OK"
    h.d.begin_utterance("9f2c1a", "quit the game")
    r = h.d.quit_game(1888160)
    assert r.ok and "quitting" in r.detail, r
    # The appid rides the verb and the turn tags it: a stop is mutating.
    assert wire[-1] == "stop 1888160 --turn 9f2c1a", wire[-1]
    assert "quit_dispatched" in h.log.events()
    gamepc.ssh = lambda cmd, **kw: "NOTRUNNING"
    r = h.d.quit_game(1)
    assert r.ok and "nothing is running" in r.detail, r
    gamepc.ssh = lambda cmd, **kw: "BUSY:42"          # a DIFFERENT game is up
    dp.library.installed_name = lambda a: {42: "Baldur's Gate 3"}.get(a)
    r = h.d.quit_game(1)
    assert not r.ok and r.earcon == "busy" and "Baldur's Gate 3" in r.detail, r
    dp.library.installed_name = lambda a: None
    gamepc.ssh = ssh_down
    assert Harness().d.quit_game(1).earcon == "fail"

    # --- nav: correlated wire per kind, NOTREADY, unknown-kind refusal --------
    h = Harness()
    wire = []
    gamepc.ssh = lambda cmd, **kw: wire.append(cmd) or "OK"
    h.d.begin_utterance("4c1d0e", "show downloads")
    assert h.d.nav("downloads").ok and wire[-1] == "nav downloads --turn 4c1d0e", wire
    assert "nav_dispatched" in h.log.events()
    assert h.d.nav("details", 400).ok and wire[-1] == "nav details 400 --turn 4c1d0e", wire
    assert h.d.nav("store", 400).ok and wire[-1] == "nav store 400 --turn 4c1d0e", wire
    assert h.d.nav("collection", "uc-abc").ok \
        and wire[-1] == "nav collection uc-abc --turn 4c1d0e", wire
    gamepc.ssh = lambda cmd, **kw: "NOTREADY"
    r = h.d.nav("downloads")
    assert not r.ok and r.earcon == "busy", r
    assert "start one first" in r.detail, r
    # Mid-start (fresh lock) is a distinct busy from no-session: the reply must
    # not tell the model to start what is already starting (2026-08-15).
    fresh_state(10)
    r = h.d.nav("downloads")
    assert not r.ok and r.earcon == "busy" and "starting" in r.detail, r
    fresh_state(None)
    # An unknown kind is refused HERE and never reaches the wire.
    wire2 = []
    gamepc.ssh = lambda cmd, **kw: wire2.append(cmd) or "OK"
    r = h.d.nav("bogus")
    assert not r.ok and r.earcon == "fail" and not wire2, (r, wire2)
    gamepc.ssh = ssh_down
    assert Harness().d.nav("downloads").earcon == "fail"

    # --- an UNREGISTERED task says so, on every verb that fires one ----------
    # The reply must name the task and the Register-ScheduledTask fix, not read
    # as a broken verb (2026-08-14).
    h = Harness()
    dp.library.installed_name = lambda a: None
    fresh_state(5)          # a live session, so play_game takes the ssh path
    for verb, task, call in (("nav", "Nav", lambda: h.d.nav("downloads")),
                             ("stop", "StopGame", lambda: h.d.quit_game(1)),
                             ("launch", "LaunchGame", lambda: h.d.play_game(1))):
        gamepc.ssh = lambda cmd, _t=task, **kw: f"NOTASK:{_t}"
        r = call()
        assert not r.ok and task in r.detail and "registered" in r.detail, (verb, r)

    time.sleep = real_sleep
    print("OK - dispatch: lock arbiter, dry-run, spawn args, volume step/clamp, "
          "mute, retry, input map, gaming-input autostart/busy/READY, "
          "ssh outcomes, play_game paths, quit_game, nav")


if __name__ == "__main__":
    main()
