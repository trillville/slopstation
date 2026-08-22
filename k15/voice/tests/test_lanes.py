"""Blind test: the two-lane rule, enforced rather than remembered.

CLAUDE.md: the chord lane (cglib, events, couch, chord_listener) is
load-bearing, runs on system python, and must stay stdlib-only; voice is an
overlay with its own venv that may depend on the chord lane, never the
reverse. Until now that lived in prose. This reads every module's TOP-LEVEL
imports with ast (no module is imported, so nothing here needs the venv, a
config, or hardware) and fails the day a pip dependency or a voice module
reaches the lane that has to survive everything. Run:
    .venv\\Scripts\\python tests\\test_lanes.py
    python tests\\test_lanes.py            (system python works too - by design)
"""
import _bootstrap  # noqa: F401
import ast
import sys
from pathlib import Path

K15 = Path(__file__).resolve().parents[2]
VOICE = K15 / "voice"

# The load-bearing lane: stdlib plus the two system-python packages doctor.py
# tells you to install (hidapi, pyserial), and each other. Nothing else.
CHORD = ("cglib.py", "events.py", "couch.py", "chord_listener.py", "verbs.py",
         "tv.py")
CHORD_EXTRAS = {"hid", "serial"}

# Everything beside them runs on system python too (doctor, the manual CLIs,
# the catalog): the same bar at module level, with requests et al. allowed only
# inside functions - which is how library.py already imports them.
SYSTEM_PYTHON_EXTRAS = CHORD_EXTRAS | {"winreg"}

# doctor.py imports these voice modules on system python (which CLI a
# workerProvider means; how to read a JWT's exp), so their top level has to
# stay stdlib + chord-lane. The coupling is documented at doctor.py's import
# site; this is what keeps it honest after the next `import requests` someone
# adds to one of them.
VOICE_IMPORTED_BY_DOCTOR = ("workers.py", "steam_session.py", "jobs.py",
                            "tracing.py")          # jobs imports tracing


def top_level_imports(path):
    """Module names imported at depth 0 - `import x`, `from x import y`, and
    the same inside a top-level try/if (none today, but cheap to cover)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []

    def visit(nodes):
        for n in nodes:
            if isinstance(n, ast.Import):
                out.extend(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                out.append(n.module.split(".")[0])
            elif isinstance(n, (ast.If, ast.Try)):
                visit(n.body)
                for h in getattr(n, "handlers", []):
                    visit(h.body)
                visit(getattr(n, "orelse", []))
    visit(tree.body)
    return out


def main():
    stdlib = set(sys.stdlib_module_names)
    chord_names = {Path(f).stem for f in CHORD}
    k15_names = {p.stem for p in K15.glob("*.py")}
    voice_names = {p.stem for p in VOICE.glob("*.py")}
    problems = []

    # 1. The chord lane imports nothing it cannot get on system python.
    for f in CHORD:
        for mod in top_level_imports(K15 / f):
            if mod not in stdlib | CHORD_EXTRAS | chord_names:
                problems.append(f"{f}: top-level import of {mod!r} - the chord "
                                f"lane is stdlib(+hid,serial) only")

    # 2. No module in k15/ reaches a voice module at top level - the overlay
    #    depends on the lane, never the reverse. (doctor.py's function-local
    #    import of workers is the one deliberate exception, and it is covered
    #    by check 4 instead.)
    for p in sorted(K15.glob("*.py")):
        for mod in top_level_imports(p):
            if mod in voice_names:
                problems.append(f"{p.name}: imports voice module {mod!r} at "
                                f"top level - dependency runs one way only")

    # 3. Every k15/*.py is importable on system python: only stdlib, the two
    #    hardware packages, winreg, and each other at the top. Third-party
    #    libraries (requests...) stay inside functions, as library.py does.
    for p in sorted(K15.glob("*.py")):
        for mod in top_level_imports(p):
            if mod not in stdlib | SYSTEM_PYTHON_EXTRAS | k15_names:
                problems.append(f"{p.name}: top-level import of {mod!r} - k15/ "
                                f"modules run on system python; import it "
                                f"inside the function that needs it")

    # 4. The voice modules doctor.py reaches from system python - and what
    #    THEY import at the top has to meet the same bar (jobs -> tracing).
    doctor_reach = {Path(f).stem for f in VOICE_IMPORTED_BY_DOCTOR}
    for f in VOICE_IMPORTED_BY_DOCTOR:
        for mod in top_level_imports(VOICE / f):
            if mod not in stdlib | k15_names | doctor_reach:
                problems.append(f"voice/{f}: top-level import of {mod!r} - "
                                f"doctor.py imports this file on system python")

    for pr in problems:
        print("FAIL", pr)
    assert not problems, f"{len(problems)} lane violation(s)"
    n = len(CHORD) + len(list(K15.glob('*.py'))) + len(VOICE_IMPORTED_BY_DOCTOR)
    print(f"OK - lanes: chord lane stdlib-only, k15/ never imports voice/, "
          f"{n} module checks")


if __name__ == "__main__":
    main()
