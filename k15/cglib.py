"""Shared pieces for the K15 couch-gaming scripts.

Everything lives beside this file: config.json, secrets.json, couch.log,
state/, and the scripts that import this.
"""
import json, os, pathlib, struct, time

import events

BASE = pathlib.Path(__file__).resolve().parent
STATE = BASE / "state"

# Valve Steam Controller Puck (as forwarded by VirtualHere).
# 0x1304 = USB_PRODUCT_VALVE_STEAM_PROTEUS_DONGLE in SDL's usb_ids.h.
VID, PID = 0x28DE, 0x1304

# Samsung Ex-Link frames: 08 22 c1 c2 c3 value + checksum,
# checksum = (0x100 - sum(first 6)) & 0xFF. Serial is 9600 baud, 8N1.
# Volume/mute family from Samsung's RS-232 worksheet. DANGER: a one-byte slip
# in this family is power_off - frozen literals, cross-checked against
# exlink_frame() by voice/tests/test_exlink.py.
EXLINK_FRAMES = {
    "power_on":  "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
    "vol_up":      "082201000100d4",
    "vol_down":    "082201000200d3",
    "mute_toggle": "082202000000d4",   # the only toggle here - power_on/off
                                       # are discrete, so safe to re-send
}

# Every frame the S90C accepts acks with exactly these three bytes.
EXLINK_ACK = "030cf1"


class ExlinkNak(RuntimeError):
    """Not EXLINK_ACK (or no answer at all): the command did not land.
    Callers abort fast; no blind retries."""


def exlink_frame(c1, c2, c3, value):
    """Build one 7-byte Ex-Link frame (hex string) with computed checksum."""
    body = bytes([0x08, 0x22, c1, c2, c3, value])
    return (body + bytes([(0x100 - sum(body)) & 0xFF])).hex()


def vol_set_frame(level):
    """Volume Direct 0-100. Clamps to the protocol range only; the
    room-protecting volumeMax clamp lives in voice dispatch."""
    return exlink_frame(0x01, 0x00, 0x00, max(0, min(100, int(level))))


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


# --- Session state (shared by couch.py and the chord listener) ----------------
LOCK = STATE / "session.lock"
LOCK_STALE_S = 300          # a live session touches the lock every few seconds
LAST_ERROR = STATE / "last_error"   # written by couch.py on launch failure
CANCEL = STATE / "cancel"   # one line: the cancelling turn (may be
                                    # empty). Written by voice end_session,
                                    # unlinked by couch.py at every launch
                                    # wait; stale copies voided at the next
                                    # launch's start.


def lock_age():
    """Seconds since the session lock was last touched, or None if no lock."""
    try:
        return time.time() - LOCK.stat().st_mtime
    except OSError:
        return None


def session_active(age=None):
    """True while a launch or a live session owns the Puck; couch.py holds the
    lock fresh from before its first side effect through teardown. Pass `age`
    to take the decision and the log field from one stat. A STALE lock
    deliberately reads as free: worst case LOCK_STALE_S of deafness, versus a
    permanently deaf chord lane."""
    if age is None:
        age = lock_age()
    return age is not None and age < LOCK_STALE_S


def _recycle_stale_lock(content):
    """Take over a stale lock, one racer at a time; True if THIS call now owns
    it. The takeover must be ONE os.replace, never unlink-then-create: an
    empty path lets a racer's exclusive create land, and two callers win.

    The guard's exclusive create (O_EXCL) serializes recyclers, the staleness
    re-check happens INSIDE it, and the guard doubles as the incoming lock the
    swap consumes. os.replace never empties the path, so no create slips inside
    the swap, and a recycler arriving after it reads a fresh LOCK and stands
    down. A guard orphaned mid-section is recycled at 10 s."""
    guard = LOCK.with_name(LOCK.name + ".recycle")
    try:
        if time.time() - guard.stat().st_mtime > 10:
            guard.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False                # someone else is recycling right now
    try:
        if session_active():
            return False            # a racer took it while we opened the guard
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None                   # Windows will not rename an open file
        # Windows needs the rename destination unopened, and a losing racer's
        # session_active() stat denies it - ~27% of swaps against a stat spin,
        # so retry. A denied swap changes nothing; only staleness must be
        # re-read, or a release landing in between puts a live lock under it.
        for _ in range(8):
            try:
                os.replace(guard, LOCK)
                guard = None        # consumed by the swap; not ours to unlink
                return True
            except OSError:
                if session_active():
                    return False    # now someone's live lock; leave it alone
        return False
    except OSError:
        return False                # guard write failed; nothing was touched
    finally:
        if fd is not None:
            os.close(fd)
        if guard is not None:
            try:
                guard.unlink(missing_ok=True)
            except OSError:
                pass


def acquire_lock(content=""):
    """Take the session lock, or answer no. True only if THIS call put the
    file there - by exclusive create, or by the atomic swap over a stale lock.
    Each is a single filesystem operation, so racing launches produce exactly
    one winner; check-then-write does not, and two Enters recycle the Puck
    claim under the live session (controller goes input-dead). `content` is
    the owner note (couch.py writes "<turn> <pid>") read back by release_lock;
    mtime stays the only datum session_active keys on."""
    LOCK.parent.mkdir(exist_ok=True)
    denied = None
    for attempt in (1, 2, 3):
        try:
            with open(LOCK, "x", encoding="utf-8") as f:
                f.write(content)
            return True
        except FileExistsError:
            denied = None
        except PermissionError as e:
            # Windows spells a RACING create as a sharing violation, not
            # FileExistsError. A real ACL problem lands here too, told apart
            # below by no lock existing once the dust settles.
            denied = e
        if session_active() or attempt == 3:
            break
        if _recycle_stale_lock(content):
            return True
    if denied is not None and not LOCK.exists():
        raise denied
    return False


def touch_lock():
    """Freshen mtime WITHOUT rewriting content: the owner note has to survive
    the session for release_lock's ownership check."""
    try:
        os.utime(LOCK)
    except OSError:
        pass


def adopt_lock(content):
    """Take over an existing lock (reconcile's resume): rewrite the owner note
    so release_lock recognizes us. Doubles as the first heartbeat."""
    try:
        LOCK.write_text(content, encoding="utf-8")
    except OSError:
        pass


def release_lock():
    """Unlink the session lock IF this process still owns it; True if it did.

    The owner note's pid is the check: a lock recycled out from under us is
    the successor's, and unlinking it would free a live session. A note with
    no readable pid releases anyway."""
    try:
        parts = LOCK.read_text(encoding="utf-8").split()
    except OSError:
        return False                # already gone: nothing to release
    if len(parts) >= 2 and parts[1] != str(os.getpid()):
        return False
    LOCK.unlink(missing_ok=True)
    return True


# --- Haptic vocabulary: one base note, count is the message -------------------
#   1 thud = launch dispatched   2 = busy (launch already active)   3 = launch failed
_THUD     = (220, 60, 90, 0, 0)
_THUD_END = (220, 60, 0, 0, 0)
PATTERN_LAUNCH = (_THUD_END,)
PATTERN_BUSY   = (_THUD, _THUD_END)
PATTERN_FAIL   = (_THUD, _THUD, _THUD_END)


def load_config():
    """The raw file read; config() is what runtime code calls."""
    return json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))


_config = None


def config():
    """This process's config.json, read once on first call."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def use_config(cfg):
    """Test seam: make config() answer `cfg` without touching the file."""
    global _config
    _config = cfg


REQUIRED_CONFIG = ("gamingPcMac", "gamingPcIp", "sshHost", "tvComPort",
                   "tvGamingCmd", "tvIdleCmd", "tvOffWhenDone")
# Missing any of these fails the voice agent at startup, not per-wake. Every
# other voice key is optional with an inert default: config.json is
# per-machine and gitignored, so a key made mandatory in code is an agent
# that will not start after a git pull.
REQUIRED_VOICE = ("wakeModel", "wakeThreshold", "holdWindowS", "followupCarryS",
                  "eotThreshold", "eagerEotThreshold", "keytermCount",
                  "fuzzyTitleThreshold", "volumeStep", "volumeMax", "ttsVoice",
                  "assistantProvider", "assistantModelAnthropic",
                  "assistantModelOpenai", "assistantReasoningEffort", "inputs",
                  "assistantWebSearch", "assistantSearchMaxUses", "location",
                  "workerProvider", "workerModelAnthropic", "workerModelOpenai",
                  "workerEffort", "workerTimeoutS", "followUpAfterAnnounce")


def missing_config(cfg, voice=False):
    """Required keys absent from cfg (top level, or its voice section)."""
    if voice:
        section = cfg.get("voice") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            return list(REQUIRED_VOICE)
        return [k for k in REQUIRED_VOICE if k not in section]
    return [k for k in REQUIRED_CONFIG if k not in cfg]


# --- state files ----------------------------------------------------------------


def load_json(path, default):
    """A JSON state file, or `default` when absent or unparseable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, obj, indent=1):
    """tmp + os.replace, so a reader never sees a partial file. The replace
    retries: Windows denies a rename onto a file another process holds open
    (doctor reads jobs.json) - see _recycle_stale_lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 7:
                raise
            time.sleep(0.05)


# --- secrets (voice lanes; chord path never needs these) ----------------------
SECRETS = BASE / "secrets.json"


def load_secrets():
    """Fail-soft: missing or malformed file = no keys = lanes disabled
    downstream, never a crash. Reads SECRETS at call time (tests re-point it)."""
    try:
        return events.load_secrets(SECRETS)
    except ValueError:
        print(f"[cglib] {SECRETS.name} is malformed - all keyed lanes disabled")
        return {}


real_key = events.real_key


def rotate_log(max_bytes=5_000_000):
    """Two-generation rotation: couch.log -> couch.log.1 past the cap. Called
    at K15 boot (reconcile) and listener startup. Writers open-append-close
    per line, so a lost rename just rotates on the next call."""
    logf = BASE / "couch.log"
    try:
        if logf.stat().st_size > max_bytes:
            os.replace(logf, BASE / "couch.log.1")
    except OSError:
        pass


class _Log:
    """Prints, appends the human line to couch.log, and emits the same event
    as JSON for the log shipper. Called as `log("event", field=value, ...)`.
    Event names are a closed vocabulary that dashboards group by and alerts
    fire on, so variable data goes in fields, never in the name. warn/error
    means the user lost something they would notice. Under the blind suite
    (env=test) the console still gets everything but couch.log does not."""

    def __init__(self, lane):
        self.lane = lane
        self._logf = BASE / "couch.log"

    def _write(self, level, event, fields):
        # The whole body is guarded, not just the I/O: every log call funnels
        # through here, so anything that raises crashes the lane.
        try:
            # level POSITIONAL on both calls - by keyword it would collide
            # with a caller field named `level` (see events.emit).
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

    # Three levels, not four; `info` is the spelled-out form of __call__. No
    # `debug`: `level` is a Loki LABEL alerts key on, so an unemitted level is
    # a permanently empty dashboard value.
    def info(self, event, /, **fields):
        self._write(events.INFO, event, fields)

    def warn(self, event, /, **fields):
        self._write(events.WARN, event, fields)

    def error(self, event, /, **fields):
        self._write(events.ERROR, event, fields)


def make_log(lane):
    """One logger per lane ('voice', 'launch', 'listener', 'library'). The lane
    is a Loki label, so the set stays small and fixed."""
    return _Log(lane)


class CapturingLog(_Log):
    """Test double with the PRODUCTION shape - same signature, same levels -
    recording instead of writing, so a change to the logging interface breaks
    the tests. Assert on events and fields, never prose."""

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
    """Send one raw Ex-Link frame (hex string); returns EXLINK_ACK, raises
    ExlinkNak on any other answer, never retries a NAK. serial imports lazily
    so a box without pyserial can still import cglib. The 1 s retry is for
    PORT CONTENTION only: couch.py and the voice agent share this port."""
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


def tv_power_state(ip, timeout=2.0, raw=False):
    """Ask the S90C whether it is on: GET /api/v2/ on port 8001 answers
    .device.PowerState = "on" | "standby". Unauthenticated, ~30 ms, answers
    from standby (2026-08-19). An accepted power_on takes ~5 s to flip the
    state, so poll rather than read once across a transition.

    None = UNKNOWN (unreachable, IP drifted, unparseable), never "off": the
    endpoint rides Wi-Fi and deeper standby depths can drop IP entirely. A set
    off for HOURS answers in 3 ms with PowerState as the empty string
    (2026-08-21); "standby" is only what a recently-used set says. raw=True
    returns the value as reported - "on" / "standby" / "" / None - instead of
    collapsing to the safe pair. Callers pick their own safe side; the voice
    ducker treats anything but "on" as do-not-touch."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/",
                                    timeout=timeout) as r:
            state = json.load(r).get("device", {}).get("PowerState")
    except Exception:
        return None
    if raw:
        return state
    return state if state in ("on", "standby") else None


def tv_volume(ip, timeout=2.0):
    """Current volume as the TV tracks it, via pairing-free UPnP
    RenderingControl (port 9197, plain SOAP, ~3 ms). With eARC soundbar output
    this number IS the bar's level - the TV mirrors CEC system audio
    (2026-08-21).

    READ half only: with eARC audio the set refuses every direct volume WRITE
    (SetVolume answers UPnP 501; Ex-Link volume frames ack, then pop "Not
    Available" on screen - 2026-08-21). Writes go through remote keys over
    CEC, voice/tv_remote.py, verified by this read. None = unknown, not zero:
    unreachable, or the DMR service asleep (it goes down with the panel,
    unlike /api/v2/ above)."""
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
