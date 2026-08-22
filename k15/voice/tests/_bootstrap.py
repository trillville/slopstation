"""Shared test setup. `import _bootstrap` first in every test.

Paths for k15/ and k15/voice/ on sys.path; events.LOG_DIR to a tempdir; a
config fixture so the suite imports on a checkout with no config.json. Helpers
replace the per-file copies of the temp-lock and fast-sleep idioms.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
VOICE = TESTS.parent
K15 = VOICE.parent
for p in (VOICE, K15, VOICE / "bench"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import events                                   # noqa: E402
import cglib                                    # noqa: E402

events.LOG_DIR = Path(tempfile.mkdtemp(prefix="cg-test-logs-"))

# config.example.json is the fixture: a test that needs specific values
# calls cglib.use_config(...) itself.
CONFIG = json.loads((K15 / "config.example.json").read_text(encoding="utf-8-sig"))
cglib.use_config(json.loads(json.dumps(CONFIG)))


def wants(*needs):
    """Skip unless this machine has every need. Needs: steam (a local Steam
    install + PowerShell), audio (real devices), models (wake models may be
    fetched). CG_TEST_HAS lists what the runner found; unset = everything."""
    have = os.environ.get("CG_TEST_HAS")
    if have is None:
        return True
    missing = [n for n in needs if n not in have.split(",")]
    if missing:
        print(f"SKIP - needs {', '.join(missing)}")
        sys.exit(0)
    return True


def fresh_state(lock_age_s=None, lock_content="x"):
    """Point every state-file constant into a new tempdir. lock_age_s seeds a
    session lock of that age (None = absent). Returns the tempdir."""
    import library
    import jobs
    import traces
    import workers
    tmp = Path(tempfile.mkdtemp(prefix="cg-test-state-"))
    cglib.STATE = tmp
    cglib.LOCK = tmp / "session.lock"
    cglib.LAST_ERROR = tmp / "last_error"
    cglib.CANCEL = tmp / "cancel"
    library.STATE = tmp
    library.LIBRARY = tmp / "library.json"
    library.META_CACHE = tmp / "metadata-cache.json"
    try:
        import steamstore
        steamstore.DEALS = tmp / "deals.json"
        steamstore.FACET_CACHE = tmp / "facet-cache.json"
        steamstore.TAGMAP = tmp / "store-tags.json"
    except ImportError:
        library.DEALS = tmp / "deals.json"
        library.FACET_CACHE = tmp / "facet-cache.json"
        library.TAGMAP = tmp / "store-tags.json"
    jobs.JOBS_FILE = tmp / "jobs.json"
    traces.DIR = tmp / "traces"
    workers.WORKER_HOME = tmp / "worker_home"
    workers.CodexWorker.LAST = workers.WORKER_HOME / ".last-message.txt"
    if lock_age_s is not None:
        cglib.LOCK.write_text(lock_content)
        old = time.time() - lock_age_s
        os.utime(cglib.LOCK, (old, old))
    return tmp


def freeze_sleep():
    """time.sleep becomes a no-op; returns the real one."""
    real = time.sleep
    time.sleep = lambda s: None
    return real
