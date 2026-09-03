"""The Steam Controller's haptics over the Puck: the interface latch, report
builders, the play engine, the thud vocabulary. Shared by the chord listener,
haptic_test, calibrate and doctor.
"""

import struct
import time
from collections.abc import Sequence

# Valve Steam Controller Puck (as forwarded by VirtualHere).
# 0x1304 = USB_PRODUCT_VALVE_STEAM_PROTEUS_DONGLE in SDL's usb_ids.h.
VID, PID = 0x28DE, 0x1304

# Input report type the controller streams; measured by calibrate.py, not a
# Valve contract - re-run it after a firmware update. Also the interface that
# takes haptic output reports.
RID_INPUT = 0x42  # ID_TRITON_CONTROLLER_STATE in SDL

# --- Triton haptic output reports ---------------------------------------------
# Layouts from SDL's steam/controller_structs.h (Nov 2024 snapshot; re-verify
# after controller firmware updates). Plain HID output reports (dev.write) on
# the same interface that streams 0x42 state reports. All u16 little-endian,
# no padding.
HAPTIC_RUMBLE = 0x80  # 10B: type u8, intensity u16, left speed u16 + gain s8, right speed u16 + gain s8
# 8B: side u8, on_us u16, off_us u16, repeat u16; zero-filled = stop tone
HAPTIC_PULSE = 0x81
HAPTIC_TONE = 0x83  # 10B: side u8, gain_db s8, freq u16, duration_ms u16, lfo_freq u16, lfo_depth u8


def tone_report(side, freq_hz, duration_ms, gain=0, lfo_freq=0, lfo_depth=0):
    return struct.pack(
        "<BBbHHHB", HAPTIC_TONE, side, gain, freq_hz, duration_ms, lfo_freq, lfo_depth
    )


def pulse_report(side, on_us, off_us, repeat):
    return struct.pack("<BBHHH", HAPTIC_PULSE, side, on_us, off_us, repeat)


def stop_report(side):
    """Zero-filled 0x81 = stop any playing tone on that side."""
    return pulse_report(side, 0, 0, 0)


def rumble_report(intensity, left_speed, left_gain, right_speed, right_gain):
    """One-shot 0x80 rumble; hardware safety-timeout stops it in ~50 ms."""
    return struct.pack(
        "<BBHHbHb",
        HAPTIC_RUMBLE,
        0,
        intensity,
        left_speed,
        left_gain,
        right_speed,
        right_gain,
    )


# --- finding the interface ----------------------------------------------------


def streams_input_reports(reads) -> bool:
    """open_streaming_interface predicate: keep the RID_INPUT streamer."""
    return reads[-1][0] == RID_INPUT


def open_streaming_interface(accept=None, timeout_s: float = 2.0):
    """Latch a Puck HID interface. The Puck exposes ~13 and some error on read;
    those are skipped. `accept(reads)` runs after every read and decides which
    interface to keep - default keeps the first that reads at all. Returns
    (device, path), or (None, None) if nothing was accepted in timeout_s.

    hid is imported here, not at module scope, so importing this module costs
    nothing on a box without the controller's HID stack."""
    import hid

    accept = accept or (lambda reads: True)
    for info in hid.enumerate(VID, PID):
        dev = None
        try:
            dev = hid.device()
            dev.open_path(info["path"])
            dev.set_nonblocking(True)
            reads = []
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                r = dev.read(64)
                if r:
                    reads.append(r)
                    if accept(reads):
                        return dev, info["path"]
                time.sleep(0.002)
        except (OSError, ValueError):
            pass  # unreadable interface - skip it
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass
    return None, None


def play_pattern(dev, steps: Sequence[tuple[int, ...]], gain: int = 0) -> None:
    """Play a haptic pattern; production and bench audition share this engine.
    steps = ((freq_hz, dur_ms, gap_after_ms, lfo_freq, lfo_depth), ...); each
    tone plays out before the next. The trailing stops are harmless if tones
    self-terminated and required if they sustained."""
    for freq, dur, gap, lfo_f, lfo_d in steps:
        for side in (0, 1):
            dev.write(tone_report(side, freq, dur, gain, lfo_f, lfo_d))
        time.sleep((dur + gap) / 1000)
    for side in (0, 1):
        dev.write(stop_report(side))


def chirp(dev, gain: int = 0, write=None) -> None:
    """Two short self-terminating tones + stops: the 'is the haptic path alive'
    stimulus doctor.py sends. `write(dev, report, label)` swaps in haptic_test's
    logged, pad-on-failure write; the default is a plain dev.write."""
    put = write or (lambda d, report, label: d.write(report))
    for freq, dur in ((440, 60), (660, 90)):
        for side in (0, 1):
            put(
                dev,
                tone_report(side, freq, dur, gain),
                f"tone {freq}Hz/{dur}ms side{side}",
            )
        time.sleep(0.07)
    for side in (0, 1):
        put(dev, stop_report(side), f"stop side{side}")


# --- Haptic vocabulary: one base note, count is the message -------------------
#   1 thud = launch dispatched   2 = busy (launch already active)   3 = launch failed
_THUD = (220, 60, 90, 0, 0)
_THUD_END = (220, 60, 0, 0, 0)
PATTERN_LAUNCH = (_THUD_END,)
PATTERN_BUSY = (_THUD, _THUD_END)
PATTERN_FAIL = (_THUD, _THUD, _THUD_END)
