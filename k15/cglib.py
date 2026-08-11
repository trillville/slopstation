"""Shared pieces for the K15 couch-gaming scripts.

Everything lives beside this file (the k15/ folder, wherever it's checked out):
config.json, secrets.json, couch.log, state/, and the scripts that import this.
"""
import json, os, pathlib, struct, time

BASE = pathlib.Path(__file__).resolve().parent

# Valve Steam Controller Puck (as forwarded by VirtualHere).
# 0x1304 = USB_PRODUCT_VALVE_STEAM_PROTEUS_DONGLE in SDL's usb_ids.h.
VID, PID = 0x28DE, 0x1304

# Samsung Ex-Link frames: 08 22 c1 c2 c3 value + checksum,
# checksum = (0x100 - sum(first 6)) & 0xFF. Serial is 9600 baud, 8N1.
# Volume/mute family from Samsung's official RS-232 worksheet (voice design
# doc has the citation). DANGER: a one-byte slip in this family is power_off -
# entries are frozen literals, cross-checked against exlink_frame() by
# voice/tests/test_exlink.py, and never hand-typed anywhere else.
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

# Bench probe only (C1 drill): status queries are contested on modern sets.
# One generous read decides real-mute-state vs software-tracked forever.
EXLINK_VOLUME_QUERY = "0822f0010000e5"

# Proven live on the S90C 2026-08-10: every accepted frame (even the query
# frame) acks with exactly these three bytes.
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


# --- Triton haptic output reports ---------------------------------------------
# Layouts verified against SDL's steam/controller_structs.h ("snapshot from
# Nov 2024 -- things may change" - re-verify after controller firmware updates)
# and SteamControllerBridge's working implementation. These are plain HID
# output reports (dev.write), sent to the same interface that streams 0x42
# state reports. All u16 little-endian, no padding.
HAPTIC_RUMBLE = 0x80   # 10B: type u8, intensity u16, left speed u16 + gain s8, right speed u16 + gain s8
HAPTIC_PULSE  = 0x81   # 8B: side u8, on_us u16, off_us u16, repeat u16; zero-filled = stop tone
HAPTIC_TONE   = 0x83   # 10B: side u8, gain_db s8, freq u16, duration_ms u16, lfo_freq u16, lfo_depth u8


def tone_report(side, freq_hz, duration_ms, gain=0, lfo_freq=0, lfo_depth=0):
    return struct.pack('<BBbHHHB', HAPTIC_TONE, side, gain, freq_hz, duration_ms,
                       lfo_freq, lfo_depth)


def pulse_report(side, on_us, off_us, repeat):
    return struct.pack('<BBHHH', HAPTIC_PULSE, side, on_us, off_us, repeat)


def stop_report(side):
    """Zero-filled 0x81 = stop any playing tone on that side (bridge-proven)."""
    return pulse_report(side, 0, 0, 0)


def rumble_report(intensity, left_speed, left_gain, right_speed, right_gain):
    """One-shot 0x80 rumble; hardware safety-timeout stops it in ~50 ms."""
    return struct.pack('<BBHHbHb', HAPTIC_RUMBLE, 0, intensity,
                       left_speed, left_gain, right_speed, right_gain)


def play_pattern(dev, steps, gain=0):
    """THE haptic playback engine - production ack, bench audition, and the
    blind quiz all use this one function, so what you audition is exactly
    what ships. steps = ((freq_hz, dur_ms, gap_after_ms, lfo_freq, lfo_depth), ...).
    Each tone plays out fully before the next starts; trailing stops are sent
    after the last (harmless if tones self-terminated, required if sustained)."""
    for freq, dur, gap, lfo_f, lfo_d in steps:
        for side in (0, 1):
            dev.write(tone_report(side, freq, dur, gain, lfo_f, lfo_d))
        time.sleep((dur + gap) / 1000)
    for side in (0, 1):
        dev.write(stop_report(side))


# --- Session state (shared by couch.py and the chord listener) ----------------
LOCK = BASE / "state" / "session.lock"
LOCK_STALE_S = 300          # a live session touches the lock every few seconds
LAST_ERROR = BASE / "state" / "last_error"   # written by couch.py on launch failure


def lock_age():
    """Seconds since the session lock was last touched, or None if no lock."""
    try:
        return time.time() - LOCK.stat().st_mtime
    except OSError:
        return None


# --- Haptic vocabulary: one base note, count is the message -------------------
#   1 thud = launch dispatched   2 = busy (launch already active)   3 = launch failed
_THUD     = (220, 60, 90, 0, 0)
_THUD_END = (220, 60, 0, 0, 0)
PATTERN_LAUNCH = (_THUD_END,)
PATTERN_BUSY   = (_THUD, _THUD_END)
PATTERN_FAIL   = (_THUD, _THUD, _THUD_END)


def load_config():
    return json.loads((BASE / "config.json").read_text())


# --- secrets (voice lanes; chord path never needs these) ----------------------
SECRETS = BASE / "secrets.json"


def load_secrets():
    """Fail-soft: missing or malformed file = no keys = lanes disabled with a
    message downstream, never a crash (utf-8-sig eats Notepad's BOM)."""
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    except ValueError:
        print(f"[cglib] {SECRETS.name} is malformed - all keyed lanes disabled")
        return {}


def real_key(value):
    """Template junk ('dg_...', 'PLACEHOLDER...') reads as absent."""
    return (isinstance(value, str) and "..." not in value
            and not value.upper().startswith("PLACEHOLDER")
            and len(value.strip()) >= 15)


def rotate_log(max_bytes=5_000_000):
    """Two-generation rotation: couch.log -> couch.log.1 once it exceeds the
    cap. Called at K15 boot (reconcile) and listener startup. Writers open-
    append-close per line, so the rename race window is negligible; a lost
    round just rotates on the next call."""
    logf = BASE / "couch.log"
    try:
        if logf.stat().st_size > max_bytes:
            os.replace(logf, BASE / "couch.log.1")
    except OSError:
        pass


def make_log(tag):
    """Logger that prints and appends to couch.log - one place to look for
    launcher and listener history alike (chord-launched consoles close with
    their session; the file survives)."""
    logf = BASE / "couch.log"

    def log(msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {msg}"
        print(line, flush=True)
        try:
            with logf.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    return log


def _exlink_txn(frame_hex, port):
    import serial
    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(frame_hex))
        return s.read(3).hex()


def exlink_send_hex(frame_hex, port):
    """Send one raw Ex-Link frame (hex string); returns EXLINK_ACK on success,
    raises ExlinkNak on any other answer (the C1 probe proved the live TV acks
    every accepted frame with 03 0c f1, so anything else means the command did
    not land - a NAK is not retried, only reported). serial is imported lazily
    so machines without pyserial can import cglib. One retry after 1 s is for
    PORT CONTENTION only: couch.py and the voice agent share this port from
    separate processes in open-write-close bursts, so a transient open
    collision gets patience."""
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


def exlink_probe(frame_hex, port):
    """Bench only: send a frame and read until the set goes quiet (64-byte
    requests, 1 s timeout per read, 5-read cap) so a multi-frame answer
    arrives whole - the first probe's read(16) filled exactly, proving
    truncation was possible. No ack validation: the whole point is to see
    the raw answer."""
    import serial
    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(frame_hex))
        out = b""
        for _ in range(5):
            chunk = s.read(64)
            if not chunk:
                break
            out += chunk
        return out.hex()


def exlink_send(name, port):
    """Send a named frame from EXLINK_FRAMES."""
    return exlink_send_hex(EXLINK_FRAMES[name], port)
