"""The TV, from the K15: Ex-Link over serial (power, inputs, the refused
volume family) and the two pairing-free HTTP reads (power state, volume).
Chord-safe: stdlib plus a lazy pyserial import. The venv-only write path over
WebSocket is voice/tv_remote.py.
"""
import json
import time

# Samsung Ex-Link frames: 08 22 c1 c2 c3 value + checksum,
# checksum = (0x100 - sum(first 6)) & 0xFF. Serial is 9600 baud, 8N1.
# Volume/mute family from Samsung's RS-232 worksheet. DANGER: a one-byte slip
# in this family is power_off - frozen literals, cross-checked against
# exlink_frame() by voice/tests/test_exlink.py.
EXLINK_FRAMES = {
    "power_on":  "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
    "vol_up":      "082201000100d4",
    "vol_down":    "082201000200d3",
    "mute_toggle": "082202000000d4",   # the only toggle here - power_on/off
                                       # are discrete, so safe to re-send
}

# Every frame the S90C accepts acks with exactly these three bytes.
EXLINK_ACK = "030cf1"


class ExlinkNak(RuntimeError):
    """Not EXLINK_ACK (or no answer at all): the command did not land.
    Callers abort fast; no blind retries."""


def exlink_frame(c1, c2, c3, value):
    """Build one 7-byte Ex-Link frame (hex string) with computed checksum."""
    body = bytes([0x08, 0x22, c1, c2, c3, value])
    return (body + bytes([(0x100 - sum(body)) & 0xFF])).hex()


def vol_set_frame(level):
    """Volume Direct 0-100. Clamps to the protocol range only; the
    room-protecting volumeMax clamp lives in voice dispatch."""
    return exlink_frame(0x01, 0x00, 0x00, max(0, min(100, int(level))))


def _exlink_txn(frame_hex, port):
    import serial
    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(frame_hex))
        return s.read(3).hex()


def exlink_send_hex(frame_hex, port):
    """Send one raw Ex-Link frame (hex string); returns EXLINK_ACK, raises
    ExlinkNak on any other answer, never retries a NAK. serial imports lazily
    so a box without pyserial can still import tv. The 1 s retry is for
    PORT CONTENTION only: couch.py and the voice agent share this port."""
    import serial
    try:
        ack = _exlink_txn(frame_hex, port)
    except serial.SerialException:
        time.sleep(1)
        ack = _exlink_txn(frame_hex, port)
    if ack != EXLINK_ACK:
        raise ExlinkNak(f"TV answered {ack or 'nothing'} (want {EXLINK_ACK}) "
                        f"for frame {frame_hex}")
    return ack


def exlink_send(name, port):
    """Send a named frame from EXLINK_FRAMES."""
    return exlink_send_hex(EXLINK_FRAMES[name], port)


def tv_power_state(ip, timeout=2.0, raw=False):
    """Ask the S90C whether it is on: GET /api/v2/ on port 8001 answers
    .device.PowerState = "on" | "standby". Unauthenticated, ~30 ms, answers
    from standby (2026-08-19). An accepted power_on takes ~5 s to flip the
    state, so poll rather than read once across a transition.

    None = UNKNOWN (unreachable, IP drifted, unparseable), never "off": the
    endpoint rides Wi-Fi and deeper standby depths can drop IP entirely. A set
    off for HOURS answers in 3 ms with PowerState as the empty string
    (2026-08-21); "standby" is only what a recently-used set says. raw=True
    returns the value as reported - "on" / "standby" / "" / None - instead of
    collapsing to the safe pair. Callers pick their own safe side; the voice
    ducker treats anything but "on" as do-not-touch."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/",
                                    timeout=timeout) as r:
            state = json.load(r).get("device", {}).get("PowerState")
    except Exception:
        return None
    if raw:
        return state
    return state if state in ("on", "standby") else None


def tv_volume(ip, timeout=2.0):
    """Current volume as the TV tracks it, via pairing-free UPnP
    RenderingControl (port 9197, plain SOAP, ~3 ms). With eARC soundbar output
    this number IS the bar's level - the TV mirrors HDMI-CEC system audio
    (2026-08-21).

    READ half only: with eARC audio the set refuses every direct volume WRITE
    (SetVolume answers UPnP 501; Ex-Link volume frames ack, then pop "Not
    Available" on screen - 2026-08-21). Writes go through remote keys over
    HDMI-CEC, voice/tv_remote.py, verified by this read. None = unknown, not
    zero: unreachable, or the TV's UPnP renderer asleep (it goes down with
    the panel, unlike /api/v2/ above)."""
    import urllib.request
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:GetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            '<InstanceID>0</InstanceID><Channel>Master</Channel>'
            '</u:GetVolume></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(
        f"http://{ip}:9197/upnp/control/RenderingControl1", data=body,
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPACTION": '"urn:schemas-upnp-org:service:'
                               'RenderingControl:1#GetVolume"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read().decode(errors="replace")
        start = out.index("<CurrentVolume>") + len("<CurrentVolume>")
        return int(out[start:out.index("</CurrentVolume>")])
    except Exception:
        return None
