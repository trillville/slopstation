"""Guard: pyflakes over every k15/voice module, failing on UNDEFINED NAMES -
the crash class py_compile misses. run_session and the --text repl open
audio/network, so no unit test exercises them and undefined-name bugs
(`titles`, `cglib`) reached the K15 that way. Run:
    .venv\\Scripts\\python tests\\test_lint.py
"""
import io
from pathlib import Path

from pyflakes import api, reporter

K15 = Path(__file__).resolve().parents[2]        # .../k15
MODULES = [K15 / n for n in
           ("cglib.py", "events.py", "couch.py", "library.py", "exlink.py",
            "chord_listener.py", "doctor.py", "calibrate.py")]
MODULES += sorted((K15 / "voice").glob("*.py"))
# bench/ too: a probe is what you reach for when something is already wrong,
# so an undefined name in one costs the diagnosis.
MODULES += sorted((K15 / "voice" / "bench").glob("*.py"))
# wake-training/ likewise: those scripts run for hours before reaching the code
# an undefined name would break. Their livekit-wakeword import is absent from
# this venv - harmless, pyflakes parses rather than imports.
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
