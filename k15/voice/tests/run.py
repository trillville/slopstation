"""Run the blind suite: one subprocess per test file, so events._env() sees
`tests/` in argv[0] and monkeypatches stay per-process.

    .venv\\Scripts\\python tests\\run.py            (skips what this machine lacks)
    .venv\\Scripts\\python tests\\run.py --all      (run everything)
    .venv\\Scripts\\python tests\\run.py test_couch test_turn

Exit code = failures. A test skips itself through _bootstrap.wants(): steam (a
local Steam + PowerShell, detected here), audio (real devices - set
CG_TEST_AUDIO=1 on the K15).
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def detect():
    have = []
    if shutil.which("powershell"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"):
                have.append("steam")
        except OSError:
            pass
    if os.environ.get("CG_TEST_AUDIO"):
        have.append("audio")
    return have


def main(argv):
    run_all = "--all" in argv
    names = [a for a in argv if not a.startswith("--")]
    files = sorted(TESTS.glob("test_*.py"))
    if names:
        files = [f for f in files if f.stem in names]
    env = dict(os.environ)
    env.pop("CG_TEST_HAS", None)             # unset = wants() answers yes to all
    if not run_all:
        env["CG_TEST_HAS"] = ",".join(detect())
    results = []
    for f in files:
        t0 = time.time()
        p = subprocess.run([sys.executable, str(f)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env, cwd=str(TESTS.parent))
        out = p.stdout.strip().splitlines()
        err = p.stderr.strip().splitlines()
        last = out[-1] if out else (err[-1] if err else "")
        status = "PASS" if p.returncode == 0 else "FAIL"
        if p.returncode == 0 and any(l.startswith("SKIP") for l in out):
            status, last = "SKIP", next(l for l in out if l.startswith("SKIP"))
        results.append((f.stem, status, time.time() - t0, last))
        if status == "FAIL":
            print(f"--- {f.stem} output:")
            print("\n".join((out + err)[-30:]))
    width = max(len(r[0]) for r in results)
    for name, status, dt, last in results:
        print(f"{status:4} {name:{width}} {dt:5.1f}s  {last[:80]}")
    fails = sum(1 for r in results if r[1] == "FAIL")
    print(f"\n{sum(1 for r in results if r[1] == 'PASS')} pass, "
          f"{sum(1 for r in results if r[1] == 'SKIP')} skip, {fails} fail")
    return fails


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
