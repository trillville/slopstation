"""The TV over IP: remote keys (the volume write path
that works on this rig) and the session ducker built on them.

With sound output on the eARC soundbar (HW-Q990C) the TV refuses every direct
volume write: Ex-Link vol frames ack and pop "Not Available", UPnP SetVolume
answers 501 (both 2026-08-21). Only remote volume keys move the bar, relayed
by the TV over HDMI-CEC; the WebSocket remote on port 8002 injects those.
Bench 2026-08-21: one KEY_VOLDOWN = one step, tracked by tv.tv_volume
within a second.

The voice lane's write path; the chord lane's own readback lives in tv.py.

The TV must allow this client once (Device Connection Manager): run `pair`
from the K15 before first use and accept the popup on the TV.

CLI (lane=manual):

    .venv\\Scripts\\python tv_remote.py pair
    .venv\\Scripts\\python tv_remote.py vol            read current volume
    .venv\\Scripts\\python tv_remote.py down|up [n]    n volume keys
"""

import sys
import time

from slopstation import cglib, events, tv

KEYS = {"down": "KEY_VOLDOWN", "up": "KEY_VOLUP"}


class TvRemote:
    """One connection per burst: open, N keys, close. The token rides
    state\\tv-ws-token.txt, written on the first allowed connect; cached, a
    connect is ~1 s and popup-free. key_delay is CEC relay pacing: 0.15 s
    matches a human holding the remote and benched clean."""

    def __init__(self, ip, name="slopstation-k15", key_delay=0.15, timeout=6):
        self.ip = ip
        self.name = name
        self.key_delay = key_delay
        self.timeout = timeout
        self.token = cglib.STATE / "tv-ws-token.txt"

    def press(self, direction, n):
        """Send n volume keys. Raises on a dead/unpaired connection."""
        if n <= 0:
            return
        from samsungtvws import SamsungTVWS

        self.token.parent.mkdir(exist_ok=True)
        ws = SamsungTVWS(
            self.ip,
            port=8002,
            token_file=str(self.token),
            name=self.name,
            timeout=self.timeout,
            key_press_delay=self.key_delay,
        )
        try:
            ws.send_key(KEYS[direction], times=n)
        finally:
            ws.close()

    def pair(self, timeout=45):
        """First connect: the TV asks the viewer to Allow; the token then
        rides self.token. Long timeout: a human has to find the popup."""
        from samsungtvws import SamsungTVWS

        self.token.parent.mkdir(exist_ok=True)
        ws = SamsungTVWS(
            self.ip,
            port=8002,
            token_file=str(self.token),
            name=self.name,
            timeout=timeout,
        )
        ws.open()
        ws.close()


class TvDucker:
    """Drop the room's volume for a voice session; restore it on close. A
    talker on the couch reaches the mic 10-20 dB below TV dialogue.

    One per agent process, synchronous, every call from the wake loop's
    duck() thread under its lock. The ledger spans sessions but dies with the
    process, so a restart between duck and unduck loses it.

    Gate: duck() runs only when tv.tv_power_state says "on" (it answers
    from standby too); anything else, unknown included, skips (2026-08-16).

    Readback: writes are remote keys over CEC (TvRemote.press, the only path
    the eARC setup honours) and the pairing-free UPnP volume (tv.tv_volume,
    mirroring the soundbar) is ground truth. The ledger holds only VERIFIED
    movement, so a shortfall carries as debt to the next close and a human
    moving the remote mid-session is detected (2026-08-21)."""

    TOPUPS = 2  # extra key rounds when the readback comes up short
    POLLS = 6  # readback polls per settle, POLL_GAP_S apart
    POLL_GAP_S = 0.4

    def __init__(
        self,
        steps,
        tv_ip,
        log,
        dry_run=False,
        to_pct=None,
        probe=None,
        read=None,
        press=None,
        pause=time.sleep,
    ):
        # to_pct (1-99) wins over steps: duck TO that percent of the pre-duck
        # level, so the drop scales with how loud the room is.
        self.steps = int(steps)
        self.to_pct = int(to_pct) if to_pct else None
        self.log = log
        self.dry_run = dry_run
        self.probe = probe or (lambda: tv.tv_power_state(tv_ip))
        self.read = read or (lambda: tv.tv_volume(tv_ip))
        self.press = press or self._ws_press(tv_ip)
        self.pause = pause
        self.out = 0  # verified steps down, not yet restored
        self.expect = None  # the readback our last op left behind

    @staticmethod
    def _ws_press(tv_ip):
        def go(direction, n):
            TvRemote(tv_ip).press(direction, n)

        return go

    def _settle(self, target):
        """Poll the readback toward target; the relay lands in ~1 s."""
        for _ in range(self.POLLS):
            v = self.read()
            if v == target:
                return v
            self.pause(self.POLL_GAP_S)
        return self.read()

    def _drive(self, direction, now, target):
        """Press toward target, verify by readback, top up what got lost.
        Returns the last level actually SEEN - `now` if nothing verified.
        Rounds are bounded so a dead relay gets bursts, not a storm."""
        best = now
        for _ in range(1 + self.TOPUPS):
            need = abs(target - best)
            if not need:
                break
            try:
                self.press(direction, need)
            except Exception as e:
                self.log.warn("tv_duck_failed", stage="press", err=str(e))
            final = self._settle(target)
            if final is None:
                break
            if final == best:
                break  # keys verifiably bought nothing: stop
            best = final
        return best

    def duck(self):
        state = self.probe()
        if state != "on":
            self.log("tv_duck_skipped", state=state or "unknown", debt=self.out)
            return
        # Owed a duck already: the last close could not reach the set, so the
        # bar is still that far below the baseline - as far as a fresh duck
        # would take it. Ducking again lands on the 0-clamp (silence);
        # repaying first swings the room up and back down at the wake. Leave
        # it, and let the close restore the whole ledger.
        if self.out:
            self.log(
                "tv_duck_skipped", state="on", reason="already_ducked", debt=self.out
            )
            return
        v0 = self.read()
        if v0 is None:
            self.log("tv_duck_skipped", state="on", reason="no_readback", debt=self.out)
            return
        if self.to_pct:
            target = min(v0, round(v0 * self.to_pct / 100))
            asked = v0 - target  # scales with v0; never clamps below 0
        else:
            target = max(0, v0 - self.steps)
            asked = self.steps  # flat; the 0-clamp shows as landed<asked
        if self.dry_run:
            self.log("dry_run_would", action=f"duck vol {v0}->{target}")
            self.out += v0 - target
            self.expect = target
            return
        final = self._drive("down", v0, target)
        landed = max(0, v0 - final)
        self.out += landed
        self.expect = final
        self.log("tv_ducked", steps=landed, asked=asked, vol=final, ok=final == target)

    def unduck(self):
        """Restore the ledger: this session's duck plus any earlier debt."""
        if not self.out:
            return
        want = self.out
        if self.dry_run:
            self.log("dry_run_would", action=f"unduck +{want}")
            self.out, self.expect = 0, None
            return
        now = self.read()
        if now is None:
            # TV gone (off, or its UPnP renderer down with the panel): keys
            # would not relay. Keep the debt; the next close retries.
            self.log("tv_unducked", steps=0, asked=want, ok=False, reason="no_readback")
            self.log.warn("tv_duck_deficit", steps=self.out)
            return
        if self.expect is not None and now != self.expect:
            # A human moved the volume mid-session: adding our delta back
            # would land above their chosen level. Stand down.
            self.log(
                "tv_unducked",
                steps=0,
                asked=want,
                ok=True,
                reason="user_adjusted",
                vol=now,
            )
            self.out, self.expect = 0, None
            return
        target = min(100, now + want)
        final = self._drive("up", now, target)
        restored = max(0, final - now)
        self.out = max(0, self.out - restored)
        self.expect = final if self.out else None
        self.log("tv_unducked", steps=restored, asked=want, vol=final, ok=self.out == 0)
        if self.out:
            self.log.warn("tv_duck_deficit", steps=self.out)


def main(argv):
    ip = cglib.config().get("tvIp")
    if not ip:
        print("config.json has no tvIp")
        return 2
    if argv and argv[0] == "vol":
        print(tv.tv_volume(ip))
        return 0
    if argv and argv[0] == "pair":
        print("watch the TV - accept the popup if one appears...")
        remote = TvRemote(ip)
        try:
            remote.pair()
            events.emit("manual", "tvremote_send", cmd="pair", ok=True)
            print(f"paired - token cached at {remote.token}")
            return 0
        except Exception as e:
            events.emit("manual", "tvremote_fail", events.ERROR, cmd="pair", err=str(e))
            print(f"pairing FAILED - {e}")
            return 1
    if argv and argv[0] in KEYS:
        n = int(argv[1]) if len(argv) > 1 else 1
        before = tv.tv_volume(ip)
        try:
            TvRemote(ip).press(argv[0], n)
        except Exception as e:
            events.emit(
                "manual", "tvremote_fail", events.ERROR, cmd=argv[0], n=n, err=str(e)
            )
            print(f"{argv[0]} x{n}: FAILED - {e}")
            return 1
        time.sleep(1.0)
        after = tv.tv_volume(ip)
        events.emit(
            "manual",
            "tvremote_send",
            cmd=argv[0],
            n=n,
            vol_before=before,
            vol_after=after,
        )
        print(f"{argv[0]} x{n}: volume {before} -> {after}")
        return 0
    print("usage: tv_remote.py pair | vol | down [n] | up [n]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
