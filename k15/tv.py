"""The Samsung S90C, from the K15: every way this box talks to the set.

Three channels, each with its own incident history, gathered here from cglib
(where they sat 360 lines apart with the lock, the haptics and the logger in
between - README's "pre-drawn fault lines" were not drawn for the TV):

  Ex-Link serial   the frozen frame table (a one-byte slip in the volume
                   family is power_off), the checksum builder, and the
                   send-and-ack transport over the COM port that couch.py
                   and the voice agent share from separate processes.
  /api/v2/ power   tv_power_state - does the set SAY it is on? The gate the
                   08-16 standby bursts bought.
  UPnP volume      tv_volume - the room's level as the TV tracks it: the READ
                   half only, because with eARC audio the set acks and then
                   refuses every direct volume WRITE (08-21). The write path
                   that works, remote keys over WebSocket, is voice-lane
                   (voice/tv_remote.py) because its library lives in the
                   venv; this readback is what verifies it.

Stdlib only, pyserial imported lazily: couch.py lives on this from system
python, and the voice lane's dispatch and ducker use the same functions - one
implementation, so swapping tv.exlink_send_hex in a test intercepts every
frame from either lane.
"""
import json
import time


# Samsung Ex-Link frames: 08 22 c1 c2 c3 value + checksum,
# checksum = (0x100 - sum(first 6)) & 0xFF. Serial is 9600 baud, 8N1.
# Volume/mute family from Samsung's official RS-232 worksheet. DANGER: a
# one-byte slip in this family is power_off - entries are frozen literals,
# cross-checked against exlink_frame() by voice/tests/test_exlink.py, and never
# hand-typed anywhere else.
EXLINK_FRAMES = {
    "power_on":  "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
    "vol_up":      "082201000100d4",
    "vol_down":    "082201000200d3",
    "mute_toggle": "082202000000d4",   # discrete mute on/off does not exist
}

# Every frame the S90C accepts acks with exactly these three bytes.
EXLINK_ACK = "030cf1"


class ExlinkNak(RuntimeError):
    """The TV answered something other than EXLINK_ACK (or nothing at all) -
    the command did not land. Callers abort fast; no blind retries."""


def exlink_frame(c1, c2, c3, value):
    """Build one 7-byte Ex-Link frame (hex string) with computed checksum."""
    body = bytes([0x08, 0x22, c1, c2, c3, value])
    return (body + bytes([(0x100 - sum(body)) & 0xFF])).hex()


def vol_set_frame(level):
    """Volume Direct 0-100 (official worksheet). Clamps to the protocol range;
    the room-protecting volumeMax clamp lives in voice dispatch, not here."""
    return exlink_frame(0x01, 0x00, 0x00, max(0, min(100, int(level))))


def _exlink_txn(frame_hex, port):
    import serial
    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(frame_hex))
        return s.read(3).hex()


def exlink_send_hex(frame_hex, port):
    """Send one raw Ex-Link frame (hex string); returns EXLINK_ACK, raises
    ExlinkNak on any other answer - the TV acks every accepted frame, so
    anything else means the command did not land. A NAK is reported, never
    retried. serial imports lazily so a machine without pyserial can still
    import tv. The 1 s retry is for PORT CONTENTION only: couch.py and the
    voice agent share this port from separate processes."""
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


def tv_power_state(ip, timeout=2.0):
    """Ask the S90C itself whether it is on: GET /api/v2/ on port 8001
    answers .device.PowerState = "on" | "standby" - unauthenticated, ~30 ms,
    and it answers from standby. Measured end to end 2026-08-19;
    docs/tv-power-detection-design.md has the full numbers, including the
    ~5 s lag between an accepted power_on and the state flipping to "on",
    so poll rather than read once when watching a transition.

    None means UNKNOWN - unreachable, IP drifted, unparseable - never
    "off": the endpoint rides Wi-Fi, and Samsung's worksheet says deeper
    standby depths can drop IP entirely. One shape of None is now measured
    (2026-08-21): a set off for HOURS still answers in 3 ms but with
    PowerState as the empty string - "standby" is only what a recently-used
    set says. Callers pick their own safe side; the voice lane's ducker
    treats anything but "on" as do-not-touch."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/",
                                    timeout=timeout) as r:
            state = json.load(r).get("device", {}).get("PowerState")
    except Exception:
        return None
    return state if state in ("on", "standby") else None


def tv_volume(ip, timeout=2.0):
    """The room's CURRENT volume as the TV tracks it, via the TV's
    pairing-free UPnP RenderingControl (port 9197, plain SOAP, ~3 ms on this
    LAN). With sound output on the eARC soundbar this number IS the
    soundbar's own level - the TV mirrors the CEC system-audio state -
    verified against the bar's on-screen number on 2026-08-21.

    This is deliberately the READ half only. SetVolume on the same service
    answers UPnP 501 on this rig, and Ex-Link volume frames ack and then
    pop "Not Available" on screen (also 2026-08-21): with eARC audio the
    set refuses every direct volume WRITE. The write path that works is
    remote keys relayed over CEC - voice/tv_remote.py - and this read is
    what verifies them.

    None = unknown, not zero: unreachable, or the DMR service asleep (it
    goes down with the panel, unlike the /api/v2/ endpoint above)."""
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
