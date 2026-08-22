"""Shared pieces for the K15 couch-gaming scripts.

Everything lives beside this file: config.json, secrets.json, couch.log,
state/, and the scripts that import this.
"""
import json, os, pathlib, time

import events

BASE = pathlib.Path(__file__).resolve().parent
STATE = BASE / "state"

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
