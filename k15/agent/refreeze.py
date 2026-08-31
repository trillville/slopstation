"""Rewrite constraints.txt from the venv running this, header intact.

    .venv\Scripts\python refreeze.py      (from k15\agent, ON THE K15)

The obvious `pip freeze > constraints.txt` gets two things wrong, both
silently: it emits pins only, so it eats the header carrying that file's own
rules, and under PowerShell 5.1 `>` writes UTF-16, which is not a pins file.

Refuses to run outside a venv - a system-python freeze would pin this box's
whole site-packages as the voice lane's constraints.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
CONSTRAINTS = HERE / "constraints.txt"


def main() -> int:
    if sys.prefix == sys.base_prefix:
        print("refuse: not a venv - run .venv\Scripts\python refreeze.py")
        return 1

    r = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        print(f"pip freeze failed: {(r.stderr or r.stdout).strip()}")
        return 1

    # The header is the leading comment block; pins carry no '#'.
    old = CONSTRAINTS.read_text(encoding="utf-8").splitlines()
    header = list(dict.fromkeys(l for l in old if l.startswith("#")))
    if not header:
        print("refuse: constraints.txt has no header to keep - restore it "
              "with `git checkout -- constraints.txt` first")
        return 1

    pins = sorted(l.strip() for l in r.stdout.splitlines() if l.strip())
    urls = [p for p in pins if " @ " in p]
    if urls:
        print("refuse: pip rejects constraints with URLs - " + ", ".join(urls))
        return 1

    # utf-8, LF, no BOM: git normalises the line endings, pip reads the rest.
    CONSTRAINTS.write_text("\n".join(header + [""] + pins) + "\n",
                           encoding="utf-8", newline="\n")
    print(f"constraints.txt: {len(header)} header lines kept, {len(pins)} pins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
