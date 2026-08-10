"""Shared pieces for the K15 couch-gaming scripts.

Everything lives beside this file (the K15 desktop): config.json, couch.log,
state/session.lock, and the scripts that import this.
"""
import json, pathlib, time

BASE = pathlib.Path(__file__).resolve().parent

# Valve Steam Controller Puck (as forwarded by VirtualHere).
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


def load_config():
    return json.loads((BASE / "config.json").read_text())


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
