"""Guard: pyflakes over every module, failing on UNDEFINED NAMES - the crash
class py_compile misses; run_session and the --text repl open audio/network,
so no unit test exercises them. In tests/ it also fails on unused imports,
which are harmless per file but regrew into a copy-pasted preamble across the
whole suite. Plus the lane rule, read off the AST: the
modules that run on system python import only stdlib + each other (+ hid and
serial) at top level, and nothing in k15/ imports voice/ - not even lazily. Run:
    .venv\\Scripts\\python tests\\test_lint.py
"""
import ast
import io
import sys

from pyflakes import api, reporter

import _bootstrap  # noqa: F401

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
# The agent skills were in no sweep at all, and nothing imports them either, so
# a name error there surfaces only when someone is already debugging.
MODULES += sorted((K15.parent / ".claude" / "skills").rglob("*.py"))
# tests/ last, and tracked separately: this suite is the only place where the
# unused half of pyflakes is also enforced (see UNUSED_EXEMPT).
TESTS = sorted(_bootstrap.TESTS.glob("*.py"))
MODULES += TESTS

# An unused import is noise anywhere, but in tests/ it was a copy-pasted
# sys/Path preamble that regrew on every new file until it reached 41 lines, so
# here it fails. _bootstrap is imported for its side effects alone - paths, a
# temp log and state dir, a config fixture - and is never referenced.
UNUSED_EXEMPT = {"_bootstrap"}

# Runs on system python (no venv): every module in k15/ except the two that
# reach venv-only packages lazily - library imports requests and
# howlongtobeatpy, steamstore imports requests. A glob minus that pair, not a
# hand-kept list: a list silently exempts whatever nobody remembered to add,
# and subtracting the pair keeps `import library` in a chord-lane module a
# FAILURE rather than a runtime death on the K15's system python.
# Third-party allowed: hid, serial.
VENV_ONLY = {"library", "steamstore"}
SYSTEM_PYTHON = {p.stem for p in K15.glob("*.py")} - VENV_ONLY
THIRD_PARTY_OK = {"hid", "serial"}
STDLIB = set(sys.stdlib_module_names)


def _imports(nodes):
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], node.lineno


def top_level_imports(path):
    return _imports(ast.parse(path.read_text(encoding="utf-8")).body)


def all_imports(path):
    return _imports(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))


def check_lanes():
    voice_mods = {p.stem for p in (K15 / "voice").glob("*.py")}
    bad = []
    for p in K15.glob("*.py"):
        for mod, line in all_imports(p):
            if mod in voice_mods:
                bad.append(f"{p.name}:{line} imports voice module {mod} (k15/ must not)")
        if p.stem in SYSTEM_PYTHON:
            for mod, line in top_level_imports(p):
                if not (mod in STDLIB or mod in SYSTEM_PYTHON or mod in THIRD_PARTY_OK):
                    bad.append(f"{p.name}:{line} imports {mod} - not stdlib, runs on system python")
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
            elif f in TESTS and "imported but unused" in line:
                if line.split("'")[1].split(".")[-1] not in UNUSED_EXEMPT:
                    problems.append(line)
    for p in problems:
        print("FAIL", p)
    assert checked >= 10, f"only {checked} modules checked - path bug, not a real pass"
    assert not problems, f"{len(problems)} pyflakes issue(s)"
    lanes = check_lanes()
    for b in lanes:
        print("FAIL", b)
    assert not lanes, f"{len(lanes)} lane-rule violation(s)"
    print(f"OK - pyflakes: no undefined names across {checked} modules, "
          f"no unused imports across {len(TESTS)} tests; "
          f"lane rule holds for {len(SYSTEM_PYTHON)} system-python modules")


if __name__ == "__main__":
    main()
