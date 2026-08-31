"""Shared test setup. `import _bootstrap` first in every test.

k15/ on sys.path - and ONLY k15/: with k15/agent/ there too, agent/tools/
library.py imports twice, as `agent.tools.library` AND as `tools.library`, and
a rebind below reaches only one of the two objects.

This directory must stay named `tests` at some level of its resolved path:
events._env() reads argv[0] for that component and everything the suite emits
is env=prod without it (test_events asserts it, so a rename fails loudly).

events.LOG_DIR and cglib's state paths to tempdirs, so no test writes beside
the real lanes whether or not it asks for one; a config fixture so the suite
imports on a checkout with no config.json. fresh_state() points every
state-file constant into a NEW tempdir; wants() skips a test the machine
cannot run.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
AGENT = TESTS.parent
K15 = AGENT.parent
if str(K15) not in sys.path:
    sys.path.insert(0, str(K15))


def agent_modules():
    """Every .py in the agent package, found by FOLLOWING __init__.py. A
    directory without one is not part of the package, which is what keeps
    agent/.venv (11k third-party files) and models/, bench/, tests/,
    __pycache__ out without naming any of them."""
    def walk(d):
        yield from sorted(d.glob("*.py"))
        for sub in sorted(d.iterdir()):
            if sub.is_dir() and (sub / "__init__.py").exists():
                yield from walk(sub)
    return list(walk(AGENT))


def modname(path):
    """Import name for a lane file: k15/cglib.py -> cglib;
    k15/agent/tools/library.py -> agent.tools.library;
    k15/agent/tools/__init__.py -> agent.tools."""
    rel = path.relative_to(K15).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)

import events
import cglib

events.LOG_DIR = Path(tempfile.mkdtemp(prefix="cg-test-logs-"))
# Before any module that derives its state paths from cglib.STATE is imported.
cglib.STATE = Path(tempfile.mkdtemp(prefix="cg-test-state-"))
cglib.LOCK = cglib.STATE / "session.lock"
cglib.LAST_ERROR = cglib.STATE / "last_error"
cglib.CANCEL = cglib.STATE / "cancel"

# config.example.json is the fixture: a test that needs specific values
# calls cglib.use_config(...) itself.
CONFIG = json.loads((K15 / "config.example.json").read_text(encoding="utf-8-sig"))
cglib.use_config(json.loads(json.dumps(CONFIG)))


def wants(*needs):
    """Skip unless this machine has every need: steam (a local Steam install +
    PowerShell, detected by run.py), audio (real devices: set CG_TEST_AUDIO=1).
    run.py passes what it found in CG_TEST_HAS; unset (a direct run, or
    run.py --all) means everything."""
    have = os.environ.get("CG_TEST_HAS")
    if have is None:
        return True
    missing = [n for n in needs if n not in have.split(",")]
    if missing:
        print(f"SKIP - needs {', '.join(missing)}")
        sys.exit(0)
    return True


def has(*needs):
    """wants() without the exit, for gating one test inside a file that
    otherwise runs blind. Same words, same CG_TEST_HAS contract."""
    have = os.environ.get("CG_TEST_HAS")
    return have is None or all(n in have.split(",") for n in needs)


def fresh_state(lock_age_s=None, lock_content="x"):
    """Point every state-file constant into a new tempdir. lock_age_s seeds a
    session lock of that age (None = absent). Returns the tempdir."""
    from agent.telemetry import traces
    from agent.tools import library, operations, steamstore
    tmp = Path(tempfile.mkdtemp(prefix="cg-test-state-"))
    cglib.STATE = tmp
    cglib.LOCK = tmp / "session.lock"
    cglib.LAST_ERROR = tmp / "last_error"
    cglib.CANCEL = tmp / "cancel"
    library.LIBRARY = tmp / "library.json"
    library.META_CACHE = tmp / "metadata-cache.json"
    steamstore.DEALS = tmp / "deals.json"
    steamstore.FACET_CACHE = tmp / "facet-cache.json"
    steamstore.TAGMAP = tmp / "store-tags.json"
    operations.OPERATIONS_FILE = tmp / "operations.json"
    traces.DIR = tmp / "traces"
    if lock_age_s is not None:
        cglib.LOCK.write_text(lock_content)
        old = time.time() - lock_age_s
        os.utime(cglib.LOCK, (old, old))
    return tmp
