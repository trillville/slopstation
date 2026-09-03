"""Guard: pyflakes over every module, failing on UNDEFINED NAMES - the crash
class py_compile misses; run_session and the --text repl open audio/network, so
no unit test exercises them. In tests/ it also fails on unused imports, which
are harmless per file but regrew into a copy-pasted preamble across the whole
suite. Run:
    pytest tests/test_lint.py
"""
import io

from pyflakes import api, reporter

import helpers

REPO = helpers.REPO
PACKAGE = helpers.PACKAGE

# The package walk follows __init__.py, so nothing outside the package - a
# .venv, a scratch directory - can wander into the sweep.
MODULES = helpers.package_modules()
# wake-training/ too: those scripts run for hours before reaching the code an
# undefined name would break. Their livekit-wakeword import is absent from this
# venv - harmless, pyflakes parses rather than imports.
MODULES += sorted((REPO / "wake-training").glob("*.py"))
# The agent skills were in no sweep at all, and nothing imports them either, so
# a name error there surfaces only when someone is already debugging.
MODULES += sorted((REPO / ".claude" / "skills").rglob("*.py"))
# tests/ last, and tracked separately: this suite is the only place where the
# unused half of pyflakes is also enforced.
TESTS = sorted((REPO / "tests").glob("*.py"))
MODULES += TESTS

# Frozen, and checked below: a subpackage that loses its __init__.py falls out
# of the walk silently, and a new one added without a line here would go
# unguarded. Keyed on __init__.py, never on "holds .py files" - the untracked
# runtime directories are not subpackages.
SUBPACKAGES = {"agent"}
AGENT_SUBPACKAGES = {"speech", "brain", "tools", "interfaces", "telemetry",
                     "bench"}


def _subpackages(root):
    return {d.name for d in root.iterdir()
            if d.is_dir() and (d / "__init__.py").exists()}


def test_lint():
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
                problems.append(line)
    for p in problems:
        print("FAIL", p)
    assert checked >= 80, f"only {checked} modules checked - path bug, not a real pass"
    # The walk follows __init__.py, so a subpackage that loses one drops out
    # SILENTLY - and `checked` above does not notice: tests/ alone clears any
    # floor a lost subpackage would breach. Count the PACKAGE, not the sweep.
    assert _subpackages(PACKAGE) == SUBPACKAGES, (
        f"slopstation subpackages are {sorted(_subpackages(PACKAGE))}, frozen "
        f"list says {sorted(SUBPACKAGES)}")
    assert _subpackages(PACKAGE / "agent") == AGENT_SUBPACKAGES, (
        f"agent subpackages are {sorted(_subpackages(PACKAGE / 'agent'))}, "
        f"frozen list says {sorted(AGENT_SUBPACKAGES)} - a lost __init__.py "
        "drops one out of the walk SILENTLY")
    assert not problems, f"{len(problems)} pyflakes issue(s)"
    print(f"OK - pyflakes: no undefined names across {checked} modules, "
          f"no unused imports across {len(TESTS)} tests")
