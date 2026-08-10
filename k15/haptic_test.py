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
  audition   labeled tour of the haptic-vocabulary candidates (run with gain 0)
  quiz       blind l/b/f discrimination test of QUIZ_SET (run with gain 0)

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


# --- Vocabulary audition / blind quiz ----------------------------------------
# A pattern = steps of (freq_hz, dur_ms, gap_after_ms, lfo_freq, lfo_depth).
# Distinguishability lives in EVENT COUNT and rhythm first (1 vs 2 vs 3),
# length second, register third - pitch *direction* alone is a weak tactile
# cue. Edit values freely; QUIZ_SET picks the trio under blind test.
PATTERNS = {
    "launch":      ((440, 60, 10, 0, 0), (660, 90, 0, 0, 0)),           # current ack: 2 quick, rising
    "busy-long":   ((220, 300, 0, 0, 0),),                              # 1 long low hum
    "busy-double": ((220, 60, 90, 0, 0), (220, 60, 0, 0, 0)),           # 2 low thuds
    "fail-triple": ((250, 100, 100, 0, 0), (220, 100, 100, 0, 0), (200, 140, 0, 0, 0)),
    "fail-insist": ((250, 100, 100, 0, 0), (220, 100, 100, 0, 0), (200, 140, 800, 0, 0),
                    (250, 100, 100, 0, 0), (220, 100, 100, 0, 0), (200, 140, 0, 0, 0)),
    "fail-rough":  ((180, 500, 0, 12, 200),),                           # LFO texture experiment
}
QUIZ_SET = {"l": "launch", "b": "busy-long", "f": "fail-insist"}


def play_pattern(dev, steps, gain, quiet=False):
    for freq, dur, gap, lfo_f, lfo_d in steps:
        for side in (0, 1):
            data = cglib.tone_report(side, freq, dur, gain, lfo_f, lfo_d)
            if quiet:
                dev.write(data)              # no logging - the quiz must not leak answers
            else:
                w(dev, data, f"tone {freq}Hz/{dur}ms side{side}")
        time.sleep((dur + gap) / 1000)
    for side in (0, 1):
        if quiet:
            dev.write(cglib.stop_report(side))
        else:
            w(dev, cglib.stop_report(side), f"stop side{side}")


def audition(dev, gain):
    """Labeled pass: learn each candidate. Run with gain 0 = production feel."""
    for name, steps in PATTERNS.items():
        print(f"\n>>> {name}")
        time.sleep(1.0)
        play_pattern(dev, steps, gain)
        time.sleep(1.5)


def quiz(dev, gain, rounds=10):
    """Blind discrimination test of QUIZ_SET. Don't look at the console while
    it plays; type l/b/f after each buzz. Confusions mean the vocabulary needs
    another iteration BEFORE any production logic gets built on it."""
    import random
    print(f"Blind test over {QUIZ_SET}. l=launch b=busy f=fail. Starting...")
    score = 0
    misses = {}
    time.sleep(2)
    for i in range(1, rounds + 1):
        key = random.choice(list(QUIZ_SET))
        time.sleep(1.0 + random.random() * 2.5)   # unpredictable onset
        play_pattern(dev, PATTERNS[QUIZ_SET[key]], gain, quiet=True)
        guess = (input(f"{i}/{rounds} which? [l/b/f] ").strip().lower() or "?")[0]
        if guess == key:
            score += 1
            print("  correct")
        else:
            misses[(key, guess)] = misses.get((key, guess), 0) + 1
            print(f"  it was {QUIZ_SET[key]}")
    print(f"\nscore {score}/{rounds}")
    for (actual, guess), n in sorted(misses.items()):
        print(f"  {QUIZ_SET[actual]} mistaken for {guess!r} x{n}")
    if not misses:
        print("  no confusions - vocabulary is distinct")


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
        "pulse": pulse, "rumble": rumble, "audition": audition, "quiz": quiz}

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
