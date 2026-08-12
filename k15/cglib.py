"""Shared pieces for the K15 couch-gaming scripts.

Everything lives beside this file (the k15/ folder, wherever it's checked out):
config.json, secrets.json, couch.log, state/, and the scripts that import this.
"""
import json, os, pathlib, struct, time

import events

BASE = pathlib.Path(__file__).resolve().parent

# Valve Steam Controller Puck (as forwarded by VirtualHere).
# 0x1304 = USB_PRODUCT_VALVE_STEAM_PROTEUS_DONGLE in SDL's usb_ids.h.
VID, PID = 0x28DE, 0x1304

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


def session_active():
    """True while a launch or a live session owns the Puck.

    THE arbiter every trigger path already consults: couch.py refuses a second
    launch on it, voice dispatch answers "busy" from it, and the chord listener
    stands off the device on it. couch.py touches the lock before its first
    side effect and every few seconds thereafter, so one predicate covers the
    whole window from dispatch through teardown.

    A STALE lock deliberately reads as free, and that bound is load-bearing now
    that the listener stands off on this: it is the only thing between a lock
    nobody cleaned up and a permanently deaf chord lane. Worst case is
    LOCK_STALE_S of deafness - exactly the bound launch_busy has always had."""
    age = lock_age()
    return age is not None and age < LOCK_STALE_S


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


class _Log:
    """Logger that prints, appends the human line to couch.log, and emits the
    same event as structured JSON for the log shipper.

    Called as `log("event_name", field=value, ...)`. The event name is a
    closed vocabulary - it is what dashboards group by and alerts fire on - so
    variable data goes in fields, never in the name. `log.warn(...)` /
    `log.error(...)` pick the level; the rule for choosing is whether the
    thing that just happened cost the user something they would notice.

    Under the blind suite (env=test) the console still gets everything, but
    couch.log does not: test output and production failures sharing one file
    in one shape is precisely the confusion this exists to end."""

    def __init__(self, lane):
        self.lane = lane
        self._logf = BASE / "couch.log"

    def _write(self, level, event, fields):
        # The whole body is guarded, not just the I/O. "Telemetry never costs
        # a session" has to be structural: this is the single choke point every
        # log call in the system funnels through, so if anything in here can
        # raise, it can crash the lane it was meant to describe - and once did.
        try:
            # level passed POSITIONALLY on both calls - see events.emit's
            # docstring. Passing it by keyword would reintroduce exactly the
            # collision this is designed out of, for a field named `level`.
            line = (f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{self.lane}] "
                    + events.human(event, level, **fields))
            try:
                print(line, flush=True)
            except (OSError, ValueError, AttributeError, UnicodeError):
                pass        # windowless task: stdout is None or a dead pipe
            if events.ENV != "test":
                try:
                    with self._logf.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass
            events.emit(self.lane, event, level, **fields)
        except Exception:
            pass

    def __call__(self, event, /, **fields):
        self._write(events.INFO, event, fields)

    def debug(self, event, /, **fields):
        self._write(events.DEBUG, event, fields)

    def info(self, event, /, **fields):
        self._write(events.INFO, event, fields)

    def warn(self, event, /, **fields):
        self._write(events.WARN, event, fields)

    def error(self, event, /, **fields):
        self._write(events.ERROR, event, fields)


def make_log(lane):
    """One logger per lane ('voice', 'launch', 'listener', 'library'). The
    lane is a Loki label, so the set stays small and fixed."""
    return _Log(lane)


class CapturingLog(_Log):
    """Test double with the PRODUCTION shape - same call signature, same
    levels - that records instead of writing.

    Shared rather than hand-rolled per test on purpose: the blind suite used
    to pass `logs.append` as a logger, which silently accepted anything and
    so could not notice the day the logging interface changed. Tests assert
    on events and fields now, never on prose, so rewording a message is free
    and renaming an event (which IS an interface - alerts group by it) is
    caught."""

    def __init__(self, lane="test", echo=False):
        super().__init__(lane)
        self.records = []
        self.echo = echo

    def _write(self, level, event, fields):
        self.records.append(dict(fields, level=level, event=event))
        if self.echo:
            print(f"[{self.lane}] " + events.human(event, level, **fields))

    def events(self):
        return [r["event"] for r in self.records]

    def find(self, event):
        return [r for r in self.records if r["event"] == event]


def _exlink_txn(frame_hex, port):
    import serial
    with serial.Serial(port, 9600, timeout=1) as s:
        s.write(bytes.fromhex(frame_hex))
        return s.read(3).hex()


def exlink_send_hex(frame_hex, port):
    """Send one raw Ex-Link frame (hex string); returns EXLINK_ACK on success,
    raises ExlinkNak on any other answer - the TV acks every accepted frame, so
    anything else means the command did not land. A NAK is not retried, only
    reported. serial is imported lazily so machines without pyserial can import
    cglib. The one retry after 1 s is for PORT CONTENTION only: couch.py and
    the voice agent share this port from separate processes in
    open-write-close bursts, so a transient open collision gets patience."""
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
