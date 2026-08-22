"""Guard: pyflakes over every k15/voice module, failing on UNDEFINED NAMES -
the crash class py_compile misses. run_session and the --text repl open
audio/network so no unit test exercises them, and two undefined-name bugs
(`titles`, `cglib`) shipped that way and only surfaced on the K15. This closes
that gap statically. Run:
    .venv\\Scripts\\python tests\\test_lint.py
"""
import io
from pathlib import Path

from pyflakes import api, reporter

K15 = Path(__file__).resolve().parents[2]        # .../k15
# Globbed, never listed: a hand-kept list is how a new k15/ module stays
# unlinted until someone remembers it. Every .py in k15/, k15/voice/ and the
# suite itself (a test with an undefined name in an unexercised branch is a
# test that will die the day that branch matters).
MODULES = sorted(K15.glob("*.py"))
MODULES += sorted((K15 / "voice").glob("*.py"))
MODULES += sorted((K15 / "voice" / "tests").glob("*.py"))
# bench/ too: a probe is the thing you reach for when something is already
# wrong, so an undefined name in one costs you the diagnosis at the worst
# moment. They were outside the sweep until a new probe went in and nothing
# noticed it had never been checked.
MODULES += sorted((K15 / "voice" / "bench").glob("*.py"))
# wake-training/ for the same reason, only more so: those scripts run for HOURS
# on the gaming PC before they reach the code an undefined name would break, so
# the cheapest possible check is worth having. They import livekit-wakeword,
# which is not in this venv - harmless, because pyflakes parses rather than
# imports, and undefined names are what it is being asked about.
MODULES += sorted((K15.parent / "wake-training").glob("*.py"))


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
    print(f"OK - pyflakes: no undefined names across {checked} modules")


if __name__ == "__main__":
    main()
