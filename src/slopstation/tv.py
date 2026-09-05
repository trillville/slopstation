"""Control TV power/input over Ex-Link and volume over HTTP."""

from __future__ import annotations

import json
import threading
import time
from typing import NamedTuple

from slopstation import paths

# All Tv instances in the voice process share the same soundbar. Reentrant so
# ducking can keep its read/restore bookkeeping in the same transaction.
_VOLUME_LOCK = threading.RLock()


class VolumeChange(NamedTuple):
    before: int
    target: int
    after: int

    @property
    def ok(self) -> bool:
        return self.after == self.target


class Tv:
    """Household TV control. Commands acknowledge receipt; volume verifies state.

    Construction performs no I/O. Each process owns its instances; the volume
    transaction coordinates instances within a process, not other processes.
    """

    def __init__(self, cfg, log):
        self.ip = cfg.get("tvIp")
        self.port = cfg.get("tvComPort")
        self.log = log

    def power_on(self, **fields) -> str:
        return self._serial("power_on", **fields)

    def power_off(self, **fields) -> str:
        return self._serial("power_off", **fields)

    def select_input(self, name: str, **fields) -> str:
        if name not in ("hdmi1", "hdmi2", "hdmi3", "hdmi4"):
            self.log.error(
                "exlink_nak", cmd=name, err=f"unknown TV input: {name}", **fields
            )
            raise ValueError(f"unknown TV input: {name}")
        return self._serial(name, **fields)

    def _serial(self, name, **fields):
        try:
            if not self.port:
                raise ValueError("TV power/input control needs tvComPort")
            ack = exlink_send(name, self.port)
            self.log("exlink_send", cmd=name, ack=ack, **fields)
            return ack
        except Exception as e:
            self.log.error("exlink_nak", cmd=name, err=str(e), **fields)
            raise

    def power_state(self, timeout=2.0, raw=False):
        return tv_power_state(self.ip, timeout=timeout, raw=raw) if self.ip else None

    def volume_transaction(self):
        """Keep a volume operation and its caller's bookkeeping together."""
        return _VOLUME_LOCK

    def volume(self):
        with self.volume_transaction():
            return tv_volume(self.ip) if self.ip else None

    def set_volume(self, level: int, maximum: int = 100) -> VolumeChange:
        return self._change_volume(level, 0, maximum)

    def adjust_volume(self, steps: int, maximum: int = 100) -> VolumeChange:
        return self._change_volume(None, steps, maximum)

    def _change_volume(self, level, steps, maximum):
        with self.volume_transaction():
            if not self.ip:
                raise ValueError("volume control needs tvIp - see setup.md")
            now = self.volume()
            if now is None:
                raise RuntimeError(
                    "couldn't read the soundbar volume - nothing changed"
                )
            asked = level if level is not None else now + steps
            target = max(0, min(100, maximum, asked))
            if target != asked:
                self.log("volume_clamped", asked=asked, set=target, max=maximum)
            if now == target:
                return VolumeChange(now, target, now)
            try:
                tv_set_volume(self.ip, target)
            except Exception as e:
                self.log.warn("tv_duck_failed", stage="write", err=str(e))
            # A lost HTTP reply does not prove the write failed.
            final = self._settle(target)
            return VolumeChange(now, target, now if final is None else final)

    def _settle(self, target):
        for _ in range(24):
            level = self.volume()
            if level == target:
                return level
            time.sleep(0.1)
        return self.volume()

    def _remote(self, timeout=6):
        if not self.ip:
            raise ValueError("remote control needs tvIp and TV pairing - see setup.md")
        from samsungtvws import SamsungTVWS

        token = paths.state("tv-ws-token.txt")
        token.parent.mkdir(exist_ok=True)
        return SamsungTVWS(
            self.ip,
            port=8002,
            token_file=str(token),
            name="slopstation-k15",
            timeout=timeout,
            key_press_delay=0.15,
        )

    def toggle_mute(self) -> None:
        """Send a toggle; receipt does not verify mute state."""
        with self._remote() as remote:
            remote.send_key("KEY_MUTE")

    def pair(self) -> None:
        with self._remote(timeout=45) as remote:
            remote.open()


def main(argv=None):
    """The normal manual controls; exlink.py remains the serial diagnostic."""
    import argparse

    from slopstation import config, events, logbook

    ap = argparse.ArgumentParser(description="Control the household TV")
    ap.add_argument(
        "command",
        choices=("power_on", "power_off", "input", "vol", "up", "down", "mute", "pair"),
    )
    ap.add_argument("value", nargs="?")
    args = ap.parse_args(argv)
    device = Tv(config.current(), logbook.logger("manual"))
    command = args.command
    n = None
    try:
        if command in ("power_on", "power_off", "input"):
            if command == "input":
                ack = device.select_input(args.value)
            else:
                ack = getattr(device, command)()
            print(f"{command}: acknowledged ({ack}); resulting state not verified")
        elif command == "vol" and args.value is None:
            print(device.volume())
        elif command in ("vol", "up", "down"):
            n = int(args.value) if args.value is not None else 1
            if command in ("up", "down") and n < 0:
                raise ValueError("steps must be nonnegative")
            change = (
                device.set_volume(n)
                if command == "vol"
                else device.adjust_volume(n if command == "up" else -n)
            )
            events.emit(
                "manual",
                "tvremote_send",
                cmd=command,
                n=n,
                vol_before=change.before,
                vol_after=change.after,
            )
            print(
                f"volume {change.before} -> {change.after}; target {change.target}, verified={change.ok}"
            )
            return 0 if change.ok else 1
        else:
            if command == "pair":
                print("watch the TV - accept the pairing prompt...")
                device.pair()
            else:
                device.toggle_mute()
            events.emit("manual", "tvremote_send", cmd=command, ok=True)
            print(f"{command}: sent")
        return 0
    except Exception as e:
        events.emit(
            "manual", "tvremote_fail", events.ERROR, cmd=command, n=n, err=str(e)
        )
        print(f"{command}: FAILED - {e}")
        return 1


# Samsung Ex-Link frames: 08 22 c1 c2 c3 value + checksum, 9600 baud 8N1.
EXLINK_FRAMES = {
    "power_on": "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
    "vol_up": "082201000100d4",
    "vol_down": "082201000200d3",
    "mute_toggle": "082202000000d4",
}

# Every frame the S90C accepts acks with exactly these three bytes.
EXLINK_ACK = "030cf1"


class ExlinkNak(RuntimeError):
    """Not EXLINK_ACK (or no answer at all): the command did not land.
    Callers abort fast; no blind retries."""


def exlink_frame(c1: int, c2: int, c3: int, value: int) -> str:
    """Build one 7-byte Ex-Link frame (hex string) with computed checksum."""
    body = bytes([0x08, 0x22, c1, c2, c3, value])
    return (body + bytes([(0x100 - sum(body)) & 0xFF])).hex()


def vol_set_frame(level: int) -> str:
    """Volume Direct 0-100. Clamps to the protocol range only; the
    room-protecting volumeMax clamp lives in voice dispatch."""
    return exlink_frame(0x01, 0x00, 0x00, max(0, min(100, int(level))))


def _exlink_txn(frame_hex: str, port: str) -> str:
    import serial

    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(frame_hex))
        return s.read(3).hex()


def exlink_send_hex(frame_hex: str, port: str) -> str:
    """Send one raw Ex-Link frame; returns EXLINK_ACK, raises ExlinkNak on any
    other answer and never retries a NAK. The one retry is for port contention
    only: couch.py and the voice agent share the port."""
    import serial

    try:
        ack = _exlink_txn(frame_hex, port)
    except serial.SerialException:
        time.sleep(1)
        ack = _exlink_txn(frame_hex, port)
    if ack != EXLINK_ACK:
        raise ExlinkNak(
            f"TV answered {ack or 'nothing'} (want {EXLINK_ACK}) for frame {frame_hex}"
        )
    return ack


def exlink_send(name: str, port: str) -> str:
    """Send a named frame from EXLINK_FRAMES."""
    return exlink_send_hex(EXLINK_FRAMES[name], port)


def tv_power_state(ip: str, timeout: float = 2.0, raw: bool = False) -> str | None:
    """The set's own word: GET /api/v2/ on port 8001 answers PowerState "on" or
    "standby", unauthenticated and from standby. An accepted power_on takes
    ~5 s to flip it, so poll across a transition.

    None is UNKNOWN (unreachable, IP drifted, unparseable), never "off": deep
    standby can drop IP entirely, and a set off for hours answers with an
    empty PowerState. raw=True returns the value as reported instead of
    collapsing it to the "on"/"standby" pair."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/", timeout=timeout) as r:
            state = json.load(r).get("device", {}).get("PowerState")
    except Exception:
        return None
    if raw:
        return state
    return state if state in ("on", "standby") else None


def _volume_request(ip: str, action: str, arguments: str, timeout: float) -> str:
    """UPnP RenderingControl on the TV; with eARC this controls the soundbar."""
    import urllib.request

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
        f"<InstanceID>0</InstanceID><Channel>Master</Channel>{arguments}"
        f"</u:{action}></s:Body></s:Envelope>"
    ).encode()
    req = urllib.request.Request(
        f"http://{ip}:9197/upnp/control/RenderingControl1",
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def tv_set_volume(ip: str, level: int, timeout: float = 2.0) -> None:
    """Set volume without pairing. Callers must verify it with tv_volume."""
    level = max(0, min(100, int(level)))
    _volume_request(ip, "SetVolume", f"<DesiredVolume>{level}</DesiredVolume>", timeout)


def tv_volume(ip: str, timeout: float = 2.0) -> int | None:
    """Read soundbar volume without pairing. None means unknown, not zero;
    the UPnP renderer may be unavailable while the TV sleeps."""
    try:
        out = _volume_request(ip, "GetVolume", "", timeout)
        start = out.index("<CurrentVolume>") + len("<CurrentVolume>")
        return int(out[start : out.index("</CurrentVolume>")])
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
