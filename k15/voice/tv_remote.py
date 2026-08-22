"""TV remote keys over IP - the volume write path that works on this rig.

With sound output on the eARC soundbar (HW-Q990C) the TV refuses every direct
volume write: Ex-Link vol frames ack and pop "Not Available", UPnP SetVolume
answers 501 (both 2026-08-21). Only remote volume keys move the bar, relayed
by the TV over CEC; the WebSocket remote on port 8002 injects those. Bench
2026-08-21: one KEY_VOLDOWN = one step, tracked by tv.tv_volume within a
second.

Voice lane only - samsungtvws lives in the voice venv, so the chord lane may
not import this (the chord-safe readback lives in tv.py).

The TV must allow this client once (Device Connection Manager): run `pair`
from the K15 before first use and accept the popup on the TV.

CLI (lane=manual):

    .venv\\Scripts\\python tv_remote.py pair
    .venv\\Scripts\\python tv_remote.py vol            read current volume
    .venv\\Scripts\\python tv_remote.py down|up [n]    n volume keys
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                    # noqa: E402
import events                                   # noqa: E402
import tv                                       # noqa: E402

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
        ws = SamsungTVWS(self.ip, port=8002, token_file=str(self.token),
                         name=self.name, timeout=self.timeout,
                         key_press_delay=self.key_delay)
        try:
            ws.send_key(KEYS[direction], times=n)
        finally:
            ws.close()


def main(argv):
    ip = cglib.config().get("tvIp")
    if not ip:
        print("config.json has no tvIp")
        return 2
    if argv and argv[0] == "vol":
        print(tv.tv_volume(ip))
        return 0
    if argv and argv[0] == "pair":
        # Long timeout: a human has to find Allow on the TV.
        print("watch the TV - accept the popup if one appears...")
        remote = TvRemote(ip, timeout=45)
        try:
            from samsungtvws import SamsungTVWS
            remote.token.parent.mkdir(exist_ok=True)
            ws = SamsungTVWS(ip, port=8002, token_file=str(remote.token),
                             name=remote.name, timeout=45)
            ws.open()
            ws.close()
            events.emit("manual", "tvremote_send", cmd="pair", ok=True)
            print(f"paired - token cached at {remote.token}")
            return 0
        except Exception as e:
            events.emit("manual", "tvremote_fail", events.ERROR, cmd="pair",
                        err=str(e))
            print(f"pairing FAILED - {e}")
            return 1
    if argv and argv[0] in KEYS:
        n = int(argv[1]) if len(argv) > 1 else 1
        before = tv.tv_volume(ip)
        try:
            TvRemote(ip).press(argv[0], n)
        except Exception as e:
            events.emit("manual", "tvremote_fail", events.ERROR, cmd=argv[0],
                        n=n, err=str(e))
            print(f"{argv[0]} x{n}: FAILED - {e}")
            return 1
        time.sleep(1.0)
        after = tv.tv_volume(ip)
        events.emit("manual", "tvremote_send", cmd=argv[0], n=n,
                    vol_before=before, vol_after=after)
        print(f"{argv[0]} x{n}: volume {before} -> {after}")
        return 0
    print("usage: tv_remote.py pair | vol | down [n] | up [n]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
