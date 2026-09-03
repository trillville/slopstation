"""Scan the repo's emitters for event names and the field keys each call site
passes. test_event_names freezes the result; nothing else imports this."""

import re

import helpers

REPO = helpers.REPO
PC = REPO / "gaming-pc"

# Names that match the call shapes but are not events.
NOT_EVENTS = {"event"}  # logbook's docstring example

# Names built at runtime, listed by hand. Field keys come from the call site.
DYNAMIC = {
    "enter_dispatched": {"dur_ms"},
    "enter_redispatched": {"dur_ms"},
    "exlink_send": {"again"},  # couch.exlink(**fields) passthrough
}
PC_DYNAMIC = {
    f"{t}_start": set()
    for t in (
        "enter",
        "exit",
        "launchgame",
        "nav",
        "stopgame",
        "office-safety",
        "wake-safety",
    )
}

# Python call shapes. Group 'name' is the event; the scan then reads the
# argument list that follows for `key=` tokens and dict-literal keys.
#   log("x" ...) / self.log("x") / self._log("x") / .warn .error
#   events.emit("lane", "x" ...) / emit(lane_var, "x" ...)   (the CLI, heartbeat)
#   emit("x" ...)   (a local bound to log or log.warn)
_PY = [
    re.compile(
        r"(?:(?<![\w.])log|self\.log|self\._log)(?:\.(?:warn|error))?"
        r"\(\s*\"(?P<name>[a-z][a-z0-9_]*)\""
    ),
    re.compile(
        r"(?<![\w.])(?:events\.)?emit\(\s*(?:\"(?P<lane>\w+)\"|\w+)\s*,\s*"
        r"\"(?P<name>[a-z][a-z0-9_]*)\""
    ),
    re.compile(r"(?<![\w.])emit\(\s*\"(?P<name>[a-z][a-z0-9_]*)\""),
]
_PS = re.compile(r"Write-(?:Cg)?Event\s+'(?P<name>[a-z][a-z0-9_]*)'")
_BAT = re.compile(
    r"slopstation\.events\s+emit\s+(?P<lane>\w+)\s+(?P<name>[a-z][a-z0-9_]*)"
)
_KEY = re.compile(r"(?<![\w.])([a-z][a-z0-9_]*)\s*=(?!=)")
_DICT_KEY = re.compile(r"\"([a-z][a-z0-9_]*)\"\s*:")


def _args(text, start):
    """The call's argument text from `start` (just past the opening paren)
    to its matching close paren."""
    depth, i = 1, start
    while i < len(text) and depth:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


def python_files():
    """Every emitter in the package. bench/ and haptic_test.py are excluded:
    they carry event names of their own ("collide", "x") and a lane literal
    "lane", which the frozen sets would then have to hold."""
    for p in helpers.package_modules():
        if p.name != "haptic_test.py" and "bench" not in p.parts:
            yield p


def scan_python():
    out = {}
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        for rx in _PY:
            for m in rx.finditer(text):
                name = m.group("name")
                if name in NOT_EVENTS:
                    continue
                args = _args(text, m.end())
                keys = set(_KEY.findall(args)) | set(_DICT_KEY.findall(args))
                out.setdefault(name, set()).update(keys)
    for name, keys in DYNAMIC.items():
        out.setdefault(name, set()).update(keys)
    return out


def scan_powershell():
    out = {}
    for path in sorted(PC.glob("*.ps1")):
        text = path.read_text(encoding="utf-8")
        for m in _PS.finditer(text):
            rest = text[m.end() : m.end() + 300]
            keys = set()
            if rest.lstrip().startswith("@{"):
                body = rest[rest.index("@{") + 2 : rest.index("}")]
                keys = set(re.findall(r"([A-Za-z_]\w*)\s*=", body))
            out.setdefault(m.group("name"), set()).update(keys)
    for name, keys in PC_DYNAMIC.items():
        out.setdefault(name, set()).update(keys)
    return out


def scan_bat():
    out = {}
    for path in sorted(REPO.glob("*.bat")):
        for m in _BAT.finditer(path.read_text(encoding="utf-8", errors="replace")):
            out.setdefault(m.group("name"), set())
    return out


def scan_lanes():
    """Lane literals: logger("...") and the events.emit("...") callers."""
    lanes = set()
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        lanes.update(re.findall(r"logger\(\"(\w+)\"\)", text))
        lanes.update(m.group("lane") for m in _PY[1].finditer(text) if m.group("lane"))
    return lanes


def scan():
    return {
        "python": scan_python(),
        "powershell": scan_powershell(),
        "bat": scan_bat(),
        "lanes": scan_lanes(),
    }


if __name__ == "__main__":
    import json

    s = scan()
    print(
        json.dumps(
            {
                k: (
                    {n: sorted(f) for n, f in sorted(v.items())}
                    if isinstance(v, dict)
                    else sorted(v)
                )
                for k, v in s.items()
            },
            indent=1,
        )
    )
