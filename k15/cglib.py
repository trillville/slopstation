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
    """THE haptic playback engine - production ack, bench audition and the
    blind quiz all use it, so what you audition is what ships.
    steps = ((freq_hz, dur_ms, gap_after_ms, lfo_freq, lfo_depth), ...). Each
    tone plays out before the next; the trailing stops are harmless if tones
    self-terminated and required if they sustained."""
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


def session_active(age=None):
    """True while a launch or a live session owns the Puck - THE arbiter, and
    the only place this predicate is spelled out: couch.py refuses a second
    launch on it, voice dispatch answers "busy" from it, doctor reports it,
    the listener stands off the device on it. couch.py holds the lock fresh
    from before its first side effect through teardown, so one predicate
    covers the whole window.

    Pass `age` when you also want the number for a log field, so the decision
    and the number come from one stat - taking two lets a lock appearing
    between them disagree, and `round(None)` in a load-bearing lane.

    A STALE lock deliberately reads as free: it is the only thing between a
    lock nobody cleaned up and a permanently deaf chord lane. Worst case is
    LOCK_STALE_S of deafness."""
    if age is None:
        age = lock_age()
    return age is not None and age < LOCK_STALE_S


def _recycle_stale_lock(content):
    """Take over a stale lock, one racer at a time; True if THIS call now owns
    it - acquire_lock's other way to win, on the same terms as its create.

    The takeover is ONE os.replace, never unlink-then-create, and that
    distinction is a bug we shipped: unlinking leaves the path EMPTY for an
    instant, a racer's exclusive create lands in it, and the next recycler -
    whose staleness test ran before that create - unlinks the fresh lock and
    creates its own. Both callers were told they won, which is the two-launch
    outcome acquire_lock exists to prevent, reached through acquire_lock.
    os.replace never empties the path, so no create can slip inside the swap.

    The guard's exclusive create serializes recyclers and the staleness
    re-check happens INSIDE it, so the file we swap out is provably the stale
    one: while it exists, no fresh lock can be created over it (acquire is
    O_EXCL). The guard doubles as the incoming lock - we won its exclusive
    create, so it is the one file no racer can hold - and the swap consumes
    it; a recycler arriving after that reads a fresh LOCK and stands down. A
    guard orphaned by a crash mid-section is recycled at 10 s."""
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
        # The swap is retried because on Windows a rename needs its
        # destination unopened, and the LOSING racer's session_active() stat
        # is enough to deny it - measured at ~27% of swaps against a stat
        # spin, so one attempt leaves both launches answering busy over a
        # lock that is stale. A denied swap changes nothing, so the only
        # thing a retry must re-read is staleness: a release landing in
        # between would put a live lock under our rename.
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
    """Take the session lock, or answer no. True only if THIS call is the one
    that put the file there - by exclusive create, or by the atomic swap that
    takes over a stale lock. Each is a single filesystem operation and THE
    arbiter on its path, so two launches racing through "looks free" still
    produce exactly one winner. Check-then-write could not say that: a chord
    landing inside the few hundred ms between a voice dispatch's check and
    couch.py's first touch let both proceed, and the second Enter recycles the
    Puck claim under the live session - the inputs-dead controller,
    manufactured by the launch path itself.

    A stale lock is taken over rather than waited out, and that takeover is a
    win: _recycle_stale_lock's True is this function's True. `content` is the
    owner note (couch.py writes "<turn> <pid>") read back by release_lock;
    mtime stays the only datum session_active and the listener key on."""
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
            # FileExistsError - same meaning, contested. A real ACL problem
            # lands here too, told apart below by no lock existing once the
            # dust settles, and THAT one deserves its crash.
            denied = e
        if session_active() or attempt == 3:
            break
        if _recycle_stale_lock(content):
            return True
    if denied is not None and not LOCK.exists():
        raise denied
    return False


def touch_lock():
    """Heartbeat: freshen mtime WITHOUT rewriting content - the owner note
    has to survive the session for release_lock's ownership check."""
    try:
        os.utime(LOCK)
    except OSError:
        pass


def adopt_lock(content):
    """Take over an existing lock (reconcile's resume): rewrite the owner
    note so release_lock recognizes us. Doubles as the first heartbeat."""
    try:
        LOCK.write_text(content, encoding="utf-8")
    except OSError:
        pass


def release_lock():
    """Unlink the session lock IF this process still owns it; True if it did.

    The owner note's pid is the check: a lock recycled out from under us (we
    stalled past LOCK_STALE_S and a new launch took over) is the successor's,
    and unlinking it would free a live session. A note with no readable pid
    releases anyway - stranding a lock over a formatting quirk is worse."""
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
    closed vocabulary - what dashboards group by and alerts fire on - so
    variable data goes in fields, never in the name. warn/error pick the
    level; the rule is whether what happened cost the user something they
    would notice.

    Under the blind suite (env=test) the console still gets everything but
    couch.log does not: test output and production failures must never share
    one file in one shape."""

    def __init__(self, lane):
        self.lane = lane
        self._logf = BASE / "couch.log"

    def _write(self, level, event, fields):
        # The whole body is guarded, not just the I/O: every log call in the
        # system funnels through here, so anything that can raise in here can
        # crash the lane it was meant to describe.
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

    # Three levels, not four. `info` is the spelled-out form of __call__, for a
    # call site where the level should be visible next to a warn/error sibling.
    # There is deliberately no `debug`: `level` is a Loki LABEL and alerts key
    # on it, so a level nothing ever emits is a value in the dashboards that
    # can only ever be empty. If a debug lane is ever wanted, add the emitter
    # and the level together.
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

    Shared rather than hand-rolled per test: a bare list-append logger
    accepts anything and so cannot notice the day the logging interface
    changes. Tests assert on events and fields, never prose, so rewording a
    message is free and renaming an event (an interface - alerts group by it)
    is caught."""

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
    ExlinkNak on any other answer - the TV acks every accepted frame, so
    anything else means the command did not land. A NAK is reported, never
    retried. serial imports lazily so a machine without pyserial can still
    import cglib. The 1 s retry is for PORT CONTENTION only: couch.py and the
    voice agent share this port from separate processes."""
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


def tv_power_state(ip, timeout=2.0):
    """Ask the S90C itself whether it is on: GET /api/v2/ on port 8001
    answers .device.PowerState = "on" | "standby" - unauthenticated, ~30 ms,
    and it answers from standby. Measured end to end 2026-08-19;
    docs/tv-power-detection-design.md has the full numbers, including the
    ~5 s lag between an accepted power_on and the state flipping to "on",
    so poll rather than read once when watching a transition.

    None means UNKNOWN - unreachable, IP drifted, unparseable - never
    "off": the endpoint rides Wi-Fi, and Samsung's worksheet says deeper
    standby depths can drop IP entirely. One shape of None is now measured
    (2026-08-21): a set off for HOURS still answers in 3 ms but with
    PowerState as the empty string - "standby" is only what a recently-used
    set says. Callers pick their own safe side; the voice lane's ducker
    treats anything but "on" as do-not-touch."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{ip}:8001/api/v2/",
                                    timeout=timeout) as r:
            state = json.load(r).get("device", {}).get("PowerState")
    except Exception:
        return None
    return state if state in ("on", "standby") else None


def tv_volume(ip, timeout=2.0):
    """The room's CURRENT volume as the TV tracks it, via the TV's
    pairing-free UPnP RenderingControl (port 9197, plain SOAP, ~3 ms on this
    LAN). With sound output on the eARC soundbar this number IS the
    soundbar's own level - the TV mirrors the CEC system-audio state -
    verified against the bar's on-screen number on 2026-08-21.

    This is deliberately the READ half only. SetVolume on the same service
    answers UPnP 501 on this rig, and Ex-Link volume frames ack and then
    pop "Not Available" on screen (also 2026-08-21): with eARC audio the
    set refuses every direct volume WRITE. The write path that works is
    remote keys relayed over CEC - voice/tv_remote.py - and this read is
    what verifies them.

    None = unknown, not zero: unreachable, or the DMR service asleep (it
    goes down with the panel, unlike the /api/v2/ endpoint above)."""
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
