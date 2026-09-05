"""Control TV power/input over Ex-Link and volume over HTTP."""

from __future__ import annotations

import json
import time

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
