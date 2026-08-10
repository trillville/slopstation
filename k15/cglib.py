"""Shared pieces for the K15 couch-gaming scripts.

Everything lives beside this file (the K15 desktop): config.json, couch.log,
state/session.lock, and the scripts that import this.
"""
import json, os, pathlib, struct, time

BASE = pathlib.Path(__file__).resolve().parent

# Valve Steam Controller Puck (as forwarded by VirtualHere).
# 0x1304 = USB_PRODUCT_VALVE_STEAM_PROTEUS_DONGLE in SDL's usb_ids.h.
VID, PID = 0x28DE, 0x1304

# Samsung Ex-Link frames: 08 22 c1 c2 c3 value + checksum,
# checksum = (0x100 - sum(first 6)) & 0xFF. Serial is 9600 baud, 8N1.
EXLINK_FRAMES = {
    "power_on":  "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
}


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


def load_config():
    return json.loads((BASE / "config.json").read_text())


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


def exlink_send(name, port):
    """Send one Ex-Link frame; returns the ack as hex ('' if none).
    serial is imported lazily so machines without pyserial can import cglib."""
    import serial
    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(EXLINK_FRAMES[name]))
        return s.read(3).hex()
