"""Haptic bench tool for the 2026 Steam Controller via the Puck.

RULE: never run this while chord_listener.py is running - one process owns the
Puck. Close the listener console first; restart it (Start-Listener.bat) when done.
Wake the controller (tap any button) before running - writes need it awake.

Usage:
    python haptic_test.py [chirp|sustained|clearmap|pulse|rumble|probe] [gain]

Subcommands, in bench order (first one that buzzes wins):
  chirp      hypothesis A: two short self-terminating 0x83 tones + stops [default]
  sustained  fallback B, bridge-proven: long tones retriggered + explicit stops
  clearmap   stock-state contingency: one-shot clear-digital-mappings feature
             report (transient, auto-restores in seconds), then the chirp
  pulse      0x81 as a real pulse train (side 0 then side 1)
  rumble     one-shot 0x80 back-motor rumble (hardware self-stops in ~50 ms)
  probe      list all Puck HID interfaces and which one streams 0x42

Optional trailing integer = gain for tone commands (s8; default 120, the
bridge-proven loud value - firmware clamps; negative attenuates).

Standing rule: after any controller firmware update, re-run calibrate.py AND
`haptic_test.py chirp` (the protocol headers are a Valve snapshot, not a contract).
"""
import sys, time

import hid

import cglib
from cglib import VID, PID

STATE_REPORT = 0x42            # ID_TRITON_CONTROLLER_STATE
SUSTAIN_MS = 0x18FF            # bridge's PlayHapticTone duration (~6.4 s)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def w(dev, data, label):
    """Write one output report and log the result. hidapi pads to the
    interface's output-report length on Windows; exact-size packets are correct."""
    n = dev.write(data)
    log(f"write {label}: {data.hex()} -> {n}")
    if n == -1:
        log("  write returned -1; retrying padded to 64 bytes")
        n = dev.write(bytes(data) + b"\x00" * (64 - len(data)))
        log(f"  padded write -> {n}")
    return n


def open_input_interface(timeout_s=2.0):
    """Latch-by-content, same as the listener and SteamControllerBridge: the
    interface that emits 0x42 state reports is also the haptic write target.
    The Puck exposes ~13 interfaces and some error on read - cull those quietly,
    exactly as the listener does."""
    for info in hid.enumerate(VID, PID):
        d = hid.device()
        try:
            d.open_path(info["path"])
            d.set_nonblocking(True)
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                r = d.read(64)
                if r and r[0] == STATE_REPORT:
                    log(f"latched 0x42 interface: {info['path']}")
                    return d
                time.sleep(0.002)
        except (OSError, ValueError):
            pass                       # unreadable interface - skip it
        try:
            d.close()
        except Exception:
            pass
    raise RuntimeError("no live 0x42 interface - controller awake? listener stopped?")


def chirp(dev, gain):
    for freq, dur in ((440, 60), (660, 90)):
        for side in (0, 1):
            w(dev, cglib.tone_report(side, freq, dur, gain), f"tone {freq}Hz/{dur}ms side{side}")
        time.sleep(0.07)
    for side in (0, 1):
        w(dev, cglib.stop_report(side), f"stop side{side}")


def sustained(dev, gain):
    for side in (0, 1):
        w(dev, cglib.tone_report(side, 440, SUSTAIN_MS, gain), f"tone 440Hz sustained side{side}")
    time.sleep(0.055)
    for side in (0, 1):
        w(dev, cglib.tone_report(side, 660, SUSTAIN_MS, gain), f"tone 660Hz sustained side{side}")
    time.sleep(0.09)
    for side in (0, 1):
        w(dev, cglib.stop_report(side), f"stop side{side}")


def clearmap(dev, gain):
    # Feature report [report_id=0x01, cmd=0x81 CLEAR_DIGITAL_MAPPINGS, len=0x00],
    # padded to 64 - format confirmed in SDL triton driver and the bridge.
    buf = bytes([0x01, 0x81, 0x00]) + b"\x00" * 61
    n = dev.send_feature_report(buf)
    log(f"feature clear-digital-mappings -> {n}")
    time.sleep(0.1)
    chirp(dev, gain)


def pulse(dev, _gain):
    for side in (0, 1):
        w(dev, cglib.pulse_report(side, 3000, 3000, 40), f"pulse side{side}")
        time.sleep(0.5)


def rumble(dev, _gain):
    w(dev, cglib.rumble_report(700, 130, 60, 270, 60), "rumble one-shot")


def probe(_dev=None, _gain=None):
    for info in hid.enumerate(VID, PID):
        log(f"path={info['path']} usage_page={info.get('usage_page', 0):#06x} "
            f"usage={info.get('usage', 0):#04x}")
    log("opening for 0x42 latch check...")
    d = open_input_interface()
    d.close()


CMDS = {"chirp": chirp, "sustained": sustained, "clearmap": clearmap,
        "pulse": pulse, "rumble": rumble}

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
