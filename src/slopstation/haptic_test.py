"""Haptic bench tool for the 2026 Steam Controller via the Puck.

Never run while chord_listener.py is running - one process owns the Puck.
Close the listener console first; Start-Slopstation.bat brings it back.
Wake the controller (tap any button) first - writes need it awake.

Usage:
    python -m slopstation.haptic_test [chirp|sustained|pulse|rumble|probe|audition] [gain]

Subcommands (try them in this order if the controller stays silent):
  chirp      two short self-terminating 0x83 tones + stops [default]
  sustained  long tones retriggered + explicit stops - the fallback if the
             self-terminating form stops working after a firmware update
  pulse      0x81 as a real pulse train (side 0 then side 1)
  rumble     one-shot 0x80 back-motor rumble (hardware self-stops in ~50 ms)
  probe      list all Puck HID interfaces and which one streams 0x42
  audition   labeled tour of the production vocabulary (run with gain 0)

Optional trailing integer = gain for tone commands (s8; default 120, a known
loud value - firmware clamps; negative attenuates).

After any controller firmware update, re-run calibrate.py AND `haptic_test.py
chirp` - the protocol headers are a Valve snapshot, not a contract.
"""

import sys
import time

import hid

from slopstation import haptics
from slopstation.haptics import PID, VID

SUSTAIN_MS = 0x18FF  # bridge's PlayHapticTone duration (~6.4 s)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def w(dev, data, label):
    """Write one output report and log the result. On Windows hidapi pads to
    the interface's output-report length, so exact-size packets are correct."""
    n = dev.write(data)
    log(f"write {label}: {data.hex()} -> {n}")
    if n == -1:
        log("  write returned -1; retrying padded to 64 bytes")
        n = dev.write(bytes(data) + b"\x00" * (64 - len(data)))
        log(f"  padded write -> {n}")
    return n


def open_input_interface(timeout_s=2.0):
    dev, path = haptics.open_streaming_interface(
        haptics.streams_input_reports, timeout_s
    )
    if not dev:
        raise RuntimeError(
            "no live 0x42 interface - controller awake? listener stopped?"
        )
    log(f"latched 0x42 interface: {path}")
    return dev


def chirp(dev, gain):
    haptics.chirp(dev, gain, write=w)


# Production vocabulary, from haptics.py.
PATTERNS = {
    "launch": haptics.PATTERN_LAUNCH,
    "busy": haptics.PATTERN_BUSY,
    "fail": haptics.PATTERN_FAIL,
}


def audition(dev, gain):
    """Labeled pass over the vocabulary; gain 0 gives the production feel."""
    for name, steps in PATTERNS.items():
        print(f"\n>>> {name}")
        time.sleep(1.0)
        haptics.play_pattern(dev, steps, gain)
        time.sleep(1.5)


def sustained(dev, gain):
    for side in (0, 1):
        w(
            dev,
            haptics.tone_report(side, 440, SUSTAIN_MS, gain),
            f"tone 440Hz sustained side{side}",
        )
    time.sleep(0.055)
    for side in (0, 1):
        w(
            dev,
            haptics.tone_report(side, 660, SUSTAIN_MS, gain),
            f"tone 660Hz sustained side{side}",
        )
    time.sleep(0.09)
    for side in (0, 1):
        w(dev, haptics.stop_report(side), f"stop side{side}")


def pulse(dev, _gain):
    for side in (0, 1):
        w(dev, haptics.pulse_report(side, 3000, 3000, 40), f"pulse side{side}")
        time.sleep(0.5)


def rumble(dev, _gain):
    w(dev, haptics.rumble_report(700, 130, 60, 270, 60), "rumble one-shot")


def probe():
    # Not in CMDS: main opens the device before dispatching one, and probe
    # enumerates for itself.
    for info in hid.enumerate(VID, PID):
        log(
            f"path={info['path']} usage_page={info.get('usage_page', 0):#06x} "
            f"usage={info.get('usage', 0):#04x}"
        )
    log("opening for 0x42 latch check...")
    d = open_input_interface()
    d.close()


CMDS = {
    "chirp": chirp,
    "sustained": sustained,
    "pulse": pulse,
    "rumble": rumble,
    "audition": audition,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "chirp"
    gain = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    if cmd == "probe":
        probe()
    elif cmd in CMDS:
        dev = open_input_interface()
        try:
            CMDS[cmd](dev, gain)
        finally:
            dev.close()
        log("done")
    else:
        print(__doc__)
        sys.exit(2)
