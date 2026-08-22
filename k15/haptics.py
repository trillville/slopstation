"""The Steam Controller's haptics over the Puck: report builders, the play
engine, the thud vocabulary. Shared by the chord listener, haptic_test,
calibrate and doctor.
"""
import struct
import time

# Valve Steam Controller Puck (as forwarded by VirtualHere).
# 0x1304 = USB_PRODUCT_VALVE_STEAM_PROTEUS_DONGLE in SDL's usb_ids.h.
VID, PID = 0x28DE, 0x1304

# Input report type the controller streams; measured by calibrate.py, not a
# Valve contract - re-run it after a firmware update. Also the interface that
# takes haptic output reports.
RID_INPUT = 0x42

# --- Triton haptic output reports ---------------------------------------------
# Layouts from SDL's steam/controller_structs.h (Nov 2024 snapshot; re-verify
# after controller firmware updates). Plain HID output reports (dev.write) on
# the same interface that streams 0x42 state reports. All u16 little-endian,
# no padding.
HAPTIC_RUMBLE = 0x80   # 10B: type u8, intensity u16, left speed u16 + gain s8, right speed u16 + gain s8
HAPTIC_PULSE  = 0x81   # 8B: side u8, on_us u16, off_us u16, repeat u16; zero-filled = stop tone
HAPTIC_TONE   = 0x83   # 10B: side u8, gain_db s8, freq u16, duration_ms u16, lfo_freq u16, lfo_depth u8


def tone_report(side, freq_hz, duration_ms, gain=0, lfo_freq=0, lfo_depth=0):
    return struct.pack('<BBbHHHB', HAPTIC_TONE, side, gain, freq_hz, duration_ms,
                       lfo_freq, lfo_depth)


def pulse_report(side, on_us, off_us, repeat):
    return struct.pack('<BBHHH', HAPTIC_PULSE, side, on_us, off_us, repeat)


def stop_report(side):
    """Zero-filled 0x81 = stop any playing tone on that side."""
    return pulse_report(side, 0, 0, 0)


def rumble_report(intensity, left_speed, left_gain, right_speed, right_gain):
    """One-shot 0x80 rumble; hardware safety-timeout stops it in ~50 ms."""
    return struct.pack('<BBHHbHb', HAPTIC_RUMBLE, 0, intensity,
                       left_speed, left_gain, right_speed, right_gain)


def play_pattern(dev, steps, gain=0):
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


# --- Haptic vocabulary: one base note, count is the message -------------------
#   1 thud = launch dispatched   2 = busy (launch already active)   3 = launch failed
_THUD     = (220, 60, 90, 0, 0)
_THUD_END = (220, 60, 0, 0, 0)
PATTERN_LAUNCH = (_THUD_END,)
PATTERN_BUSY   = (_THUD, _THUD_END)
PATTERN_FAIL   = (_THUD, _THUD, _THUD_END)
