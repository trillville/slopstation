"""Run the blind suite: every tests/test_*.py, each in its own process.

    .venv\\Scripts\\python tests\\run.py                 the pure subset
    .venv\\Scripts\\python tests\\run.py --with audio,mic  plus those features
    .venv\\Scripts\\python tests\\run.py --all            everything (the K15)
    .venv\\Scripts\\python tests\\run.py couch turn       just these two

One process per file, not an in-process import: events._env() decides
env=test from sys.argv[0] and the files monkeypatch module globals freely, so
they were written to own a process - the runner keeps that contract rather
than fighting it. A file's `REQUIRES = {...}` (see _bootstrap) is read off its
text, never by importing it; the runner passes the allowed features down as
CG_TEST_WITH so a partially-hardware file can gate its own cases.

Exit code = number of failures, like doctor.py. A file with no REQUIRES that
is not in the list of names you asked for is still a skip, never a failure.
"""
import argparse
import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRES_RE = re.compile(r"^REQUIRES\s*=\s*(\{.*\})\s*(#.*)?$", re.MULTILINE)


def requires_of(path):
    """The file's declared feature set, or an empty set. Literal-evaluated off
    the source so a test is never imported (and run) to ask what it needs."""
    head = path.read_text(encoding="utf-8")[:6000]
    m = REQUIRES_RE.search(head)
    if not m:
        return set()
    try:
        return set(ast.literal_eval(m.group(1)))
    except (ValueError, SyntaxError):
        return {"unparseable-REQUIRES"}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="*",
                    help="test names (with or without test_/.py); default all")
    ap.add_argument("--with", dest="allow", default="",
                    help="comma-separated features to allow, e.g. audio,mic")
    ap.add_argument("--all", action="store_true", help="allow every feature")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-file seconds before it counts as failed")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only the per-file verdict lines, no captured output")
    a = ap.parse_args(argv)

    allowed = {f.strip() for f in a.allow.split(",") if f.strip()}
    files = sorted(HERE.glob("test_*.py"))
    if a.names:
        want = {n.removeprefix("test_").removesuffix(".py") for n in a.names}
        files = [f for f in files if f.stem.removeprefix("test_") in want]
        missing = want - {f.stem.removeprefix("test_") for f in files}
        if missing:
            print(f"no such test(s): {', '.join(sorted(missing))}")
            return 2

    env = dict(os.environ)
    # Always set under the runner, so wants() answers "no" to anything that
    # was not asked for - by hand (unset) it answers "yes" to everything.
    env["CG_TEST_WITH"] = "*" if a.all else ",".join(sorted(allowed))
    passed, failed, skipped = [], [], []
    t_all = time.time()
    for f in files:
        need = requires_of(f)
        if need and not a.all and not need <= allowed:
            skipped.append((f.stem, need))
            print(f"SKIP {f.stem}  (needs {', '.join(sorted(need))})")
            continue
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, str(f)], cwd=str(HERE.parent),
                               capture_output=True, text=True, env=env,
                               encoding="utf-8", errors="replace",
                               timeout=a.timeout)
            rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired as e:
            rc, out, err = -1, "", f"TIMEOUT after {a.timeout}s"
        secs = time.time() - t0
        # The verdict line is the test's own last print (stdout); stderr is
        # where pipecat's import banner lands, and it must not stand in for it.
        last = next((l for l in reversed((out or err).splitlines()) if l.strip()), "")
        if rc == 0:
            passed.append(f.stem)
            print(f"ok   {f.stem}  {secs:5.1f}s  {last[:100]}")
        else:
            failed.append(f.stem)
            print(f"FAIL {f.stem}  {secs:5.1f}s  rc={rc}")
            if not a.quiet:
                tail = (out + ("\n--- stderr ---\n" + err if err.strip() else ""))
                print("\n".join("     | " + l for l in tail.splitlines()[-40:]))
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped "
          f"in {time.time() - t_all:.0f}s")
    if failed:
        print("failed: " + ", ".join(failed))
    if skipped and not a.all:
        feats = sorted({x for _, need in skipped for x in need})
        print(f"skipped need: {', '.join(feats)}  (run.py --with "
              f"{','.join(feats)}, or --all)")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
