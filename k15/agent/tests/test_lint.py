"""Guard: pyflakes over every module, failing on UNDEFINED NAMES - the crash
class py_compile misses; run_session and the --text repl open audio/network,
so no unit test exercises them. In tests/ it also fails on unused imports,
which are harmless per file but regrew into a copy-pasted preamble across the
whole suite. Plus the lane rule, read off the AST: the
modules that run on system python import only stdlib + each other (+ hid and
serial) at top level, and nothing in k15/ imports the agent lane - not even lazily. Run:
    .venv\\Scripts\\python tests\\test_lint.py
"""
import ast
import io
import sys

from pyflakes import api, reporter

import _bootstrap  # noqa: F401

K15 = _bootstrap.K15
AGENT = _bootstrap.AGENT
MODULES = sorted(K15.glob("*.py"))
# The package walk, NOT rglob: agent\.venv holds ~11.6k third-party .py and
# pyflakes reports undefined names in most of them.
MODULES += _bootstrap.agent_modules()
# bench/ too: a probe is what you reach for when something is already wrong,
# so an undefined name in one costs the diagnosis. Not a package, so the walk
# above skips it and it is listed by hand.
MODULES += sorted((AGENT / "bench").glob("*.py"))
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

# Runs on system python (no venv): EVERY module left in k15/. library and
# steamstore were the two exceptions - they reach requests lazily - and they
# moved into the agent lane, so there is nothing left to subtract.
# Third-party allowed: hid, serial.
SYSTEM_PYTHON = {p.stem for p in K15.glob("*.py")}
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


# One package name, not a set of stems: _imports yields the top-level token, so
# `import agent.tools.media`, `from agent.tools import media` and `from agent
# import tools` all collapse to "agent", and a submodule added tomorrow is
# covered the day it is written.
AGENT_PKG = "agent"


def lane_violations(name, tree):
    return [f"{name}:{line} imports the agent lane ({mod}) - k15/ must not"
            for mod, line in _imports(ast.walk(tree)) if mod == AGENT_PKG]


def check_bare_lane_imports():
    """The agent lane must reach itself ONLY through `agent.`. The supervisor
    runs voice_agent.py with cwd=k15\agent, so k15\agent is on sys.path too
    and a bare `from speech import earcons` resolves - to a SECOND module
    object, which a test rebind then misses. Measured, not theorised."""
    own = {p.stem for p in _bootstrap.agent_modules()} - {"__init__"}
    own |= {d.name for d in AGENT.iterdir()
            if d.is_dir() and (d / "__init__.py").exists()}
    bad = []
    for f in _bootstrap.agent_modules() + sorted((AGENT / "bench").glob("*.py")):
        for mod, line in _imports(ast.walk(ast.parse(f.read_text(encoding="utf-8")))):
            if mod in own:
                bad.append(f"{f.relative_to(K15)}:{line} imports {mod} bare - "
                           "use agent.<subpackage>, or it loads twice")
    return bad


def check_lanes():
    # Both halves of the vacuity guard: the package has to be there, and the
    # rule has to fire on it.
    assert (AGENT / "__init__.py").exists(),         f"no {AGENT}\__init__.py - the lane rule would pass vacuously"
    assert lane_violations("probe.py", ast.parse("import agent.tools.media")),         "lane rule does not flag `import agent.tools.media` - the rule is dead"
    bad = []
    for p in K15.glob("*.py"):
        bad += lane_violations(p.name, ast.parse(p.read_text(encoding="utf-8")))
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
    assert checked >= 80, f"only {checked} modules checked - path bug, not a real pass"
    # The walk follows __init__.py, so a subpackage that loses one drops out
    # SILENTLY - and `checked` above does not notice: tests/ alone clears any
    # floor a lost subpackage would breach. Count the PACKAGE, not the sweep.
    package = _bootstrap.agent_modules()
    assert len(package) >= 30, (
        f"only {len(package)} files in the agent package - a subpackage lost "
        "its __init__.py and fell out of the walk")
    assert not problems, f"{len(problems)} pyflakes issue(s)"
    lanes = check_lanes() + check_bare_lane_imports()
    for b in lanes:
        print("FAIL", b)
    assert not lanes, f"{len(lanes)} lane-rule violation(s)"
    print(f"OK - pyflakes: no undefined names across {checked} modules, "
          f"no unused imports across {len(TESTS)} tests; "
          f"lane rule holds for {len(SYSTEM_PYTHON)} system-python modules")


if __name__ == "__main__":
    main()
