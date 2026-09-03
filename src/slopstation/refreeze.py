r"""Rewrite constraints.txt from the venv running this, header intact.

    slopstation-refreeze        (on the K15, in its venv)

The obvious `pip freeze > constraints.txt` gets two things wrong, both
silently: it emits pins only, so it eats the header carrying that file's own
rules, and under PowerShell 5.1 `>` writes UTF-16, which is not a pins file.

Refuses to run outside a venv - a system-python freeze would pin this box's
whole site-packages - and off cp313, because a cp311 freeze silently drops
audioop-lts and downgrades pins to whatever that box resolved.
"""

from __future__ import annotations

import subprocess
import sys

from slopstation import paths

CONSTRAINTS = paths.HOME / "constraints.txt"


def main() -> int:
    if sys.prefix == sys.base_prefix:
        print(r"refuse: not a venv - run .venv\Scripts\slopstation-refreeze")
        return 1

    # The K15's interpreter, mirrored in ci.yml and pyproject.toml. Any other
    # version resolves a different set and writes it over the one CD installs
    # from.
    if sys.version_info[:2] != (3, 13):
        print(
            f"refuse: python {sys.version_info.major}.{sys.version_info.minor}"
            " - constraints.txt is the K15's cp313 freeze; run this there"
        )
        return 1

    r = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode:
        print(f"pip freeze failed: {(r.stderr or r.stdout).strip()}")
        return 1

    # The header is the leading comment block; pins carry no '#'.
    old = CONSTRAINTS.read_text(encoding="utf-8").splitlines()
    header = list(dict.fromkeys(line for line in old if line.startswith("#")))
    if not header:
        print(
            "refuse: constraints.txt has no header to keep - restore it "
            "with `git checkout -- constraints.txt` first"
        )
        return 1

    # The editable install of this package shows up as `-e <path>`, and a
    # constraint may not carry a URL, so pip's own lines for it are dropped.
    pins = sorted(
        line.strip()
        for line in r.stdout.splitlines()
        if line.strip() and not line.startswith(("-e", "#"))
    )
    urls = [p for p in pins if " @ " in p]
    if urls:
        print("refuse: pip rejects constraints with URLs - " + ", ".join(urls))
        return 1

    # utf-8, LF, no BOM: git normalises the line endings, pip reads the rest.
    CONSTRAINTS.write_text(
        "\n".join(header + [""] + pins) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"constraints.txt: {len(header)} header lines kept, {len(pins)} pins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
