"""Blind test: every module imports, and none of them reads config.json or
secrets.json to do it.

Importing is the one thing every entry point, test and bench does before
anything else, and until now nothing exercised it as a whole: couch.py read
config.json at import, so `voice_agent.py --devices` on a box without one died
with a traceback before main() could say so, and two modules hid their imports
inside functions to dodge it. This imports each k15/ and k15/voice/ module in
one process with the loaders replaced by a tripwire, so an import-time read
fails here rather than on a fresh checkout. Run:
    .venv\\Scripts\\python tests\\test_imports.py
"""
import _bootstrap  # noqa: F401
import importlib
import sys
import types
from pathlib import Path

import cglib

K15 = Path(__file__).resolve().parents[2]
VOICE = K15 / "voice"

# The listener's HID dependency is deliberately not in the voice venv (it is a
# system-python package); stub the module so the import itself can be drilled.
sys.modules.setdefault("hid", types.SimpleNamespace(
    enumerate=lambda *a, **k: [], device=object))


def main():
    reads = []

    def tripwire(name):
        def fn(*a, **k):
            reads.append(name)
            raise AssertionError(f"{name} read at import time")
        return fn
    cglib.load_config = tripwire("config.json")
    cglib.load_secrets = tripwire("secrets.json")

    names = sorted(p.stem for p in list(K15.glob("*.py")) + list(VOICE.glob("*.py")))
    failed = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:              # AssertionError from the tripwire included
            failed.append(f"{name}: {type(e).__name__}: {e}")
    for f in failed:
        print("FAIL", f)
    assert not failed, f"{len(failed)} module(s) failed to import cleanly"
    assert not reads, f"import-time reads: {reads}"
    print(f"OK - imports: {len(names)} modules import with no config/secrets read")


if __name__ == "__main__":
    main()
