"""Guard: pyflakes over every module, failing on UNDEFINED NAMES - the crash
class py_compile misses; run_session and the --text repl open audio/network,
so no unit test exercises them. Plus the lane rule, read off the AST: the
modules that run on system python import only stdlib + each other (+ hid and
serial) at top level, and nothing in k15/ imports voice/ at top level. Run:
    .venv\Scripts\python tests\test_lint.py
"""
import ast
import io
import sys
from pathlib import Path

from pyflakes import api, reporter

import _bootstrap                               # noqa: F401,E402

K15 = _bootstrap.K15
MODULES = sorted(K15.glob("*.py"))
MODULES += sorted((K15 / "voice").glob("*.py"))
# bench/ too: a probe is what you reach for when something is already wrong,
# so an undefined name in one costs the diagnosis.
MODULES += sorted((K15 / "voice" / "bench").glob("*.py"))
# wake-training/ likewise: those scripts run for hours before reaching the code
# an undefined name would break. Their livekit-wakeword import is absent from
# this venv - harmless, pyflakes parses rather than imports.
MODULES += sorted((K15.parent / "wake-training").glob("*.py"))

# Runs on system python (no venv): the chord lane plus the K15 tools, and
# voice/workers.py because doctor imports it. Third-party allowed: hid, serial.
SYSTEM_PYTHON = {"cglib", "events", "couch", "chord_listener", "exlink", "tv",
                 "haptics", "gamepc", "doctor", "calibrate", "haptic_test"}
SYSTEM_PYTHON_VOICE = {"workers"}
THIRD_PARTY_OK = {"hid", "serial"}
STDLIB = set(sys.stdlib_module_names)


def top_level_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], node.lineno


def check_lanes():
    voice_mods = {p.stem for p in (K15 / "voice").glob("*.py")}
    bad = []
    for p in K15.glob("*.py"):
        for mod, line in top_level_imports(p):
            if mod in voice_mods and mod not in SYSTEM_PYTHON_VOICE:
                bad.append(f"{p.name}:{line} imports voice module {mod} (chord lane must not)")
            if p.stem in SYSTEM_PYTHON and not (mod in STDLIB or mod in SYSTEM_PYTHON
                                                or mod in THIRD_PARTY_OK):
                bad.append(f"{p.name}:{line} imports {mod} - not stdlib, runs on system python")
    for name in SYSTEM_PYTHON_VOICE:
        p = K15 / "voice" / f"{name}.py"
        for mod, line in top_level_imports(p):
            if not (mod in STDLIB or mod in SYSTEM_PYTHON or mod in SYSTEM_PYTHON_VOICE):
                bad.append(f"voice/{p.name}:{line} imports {mod} - doctor imports this on system python")
    return bad


def main():
    problems, checked = [], 0
    for f in MODULES:
        if not f.exists():
            continue
        checked += 1
        out = io.StringIO()
        api.checkPath(str(f), reporter.Reporter(out, out))
        for line in out.getvalue().splitlines():
            if "undefined name" in line or "may be undefined" in line:
                problems.append(line)
    for p in problems:
        print("FAIL", p)
    assert checked >= 10, f"only {checked} modules checked - path bug, not a real pass"
    assert not problems, f"{len(problems)} undefined-name issue(s)"
    lanes = check_lanes()
    for b in lanes:
        print("FAIL", b)
    assert not lanes, f"{len(lanes)} lane-rule violation(s)"
    print(f"OK - pyflakes: no undefined names across {checked} modules; "
          f"lane rule holds for {len(SYSTEM_PYTHON) + len(SYSTEM_PYTHON_VOICE)} system-python modules")


if __name__ == "__main__":
    main()
