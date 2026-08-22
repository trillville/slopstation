"""The one home for what every blind test used to repeat.

    import _bootstrap                      # first line after the docstring

A test runs as a script (tests/run.py spawns one process per file, and
CLAUDE.md's `.venv\\Scripts\\python tests\\test_x.py` is the same thing by
hand), so tests/ is sys.path[0] and this resolves before any repo import.
Importing it does three things:

  * puts k15/ and k15/voice/ on sys.path - the production modules are flat
    script directories, not a package (README § Deliberately not doing), and
    this is the ONE place that knows it instead of twenty-six;
  * points events.LOG_DIR at a fresh temp dir, so test events can never land
    beside production ones whatever env detection decides (events._env keys
    on argv[0], which is why the suite runs as scripts and not under pytest);
  * offers the helpers the files kept re-rolling: fresh_state(),
    freeze_sleep(), wants().

REQUIRES / wants(): a file that needs hardware or the world declares
`REQUIRES = {"audio", ...}` at module level and run.py skips it unless asked
(`--with audio` / `--all`). A file that is MOSTLY pure with one hardware case
guards that case with `if _bootstrap.wants("mic"):` - true when run by hand
(today's behaviour, nothing changes for the person at the keyboard) and only
when the runner allowed that feature otherwise. Feature names are free text;
the ones in use: audio (plays sound), mic, downloads, network, steam (a local
Steam install), puck.
"""
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # k15/voice
sys.path.insert(0, str(HERE.parents[1]))        # k15

import events                                   # noqa: E402

events.LOG_DIR = Path(tempfile.mkdtemp(prefix="cg-test-events-"))

import cglib                                    # noqa: E402  (after LOG_DIR:
#                                    a module-level logger must never be built
#                                    against the live directory, even briefly)


def wants(feature):
    """True when the caller may exercise `feature`. By hand (no runner) that
    is always; under run.py only when it was asked for - see the module doc."""
    allowed = os.environ.get("CG_TEST_WITH")
    if allowed is None:
        return True
    return feature in {a.strip() for a in allowed.split(",") if a.strip()}


def fresh_state(lock_age_s=None, lock_content="x"):
    """Point cglib's session lock + last_error into a new tmpdir. lock_age_s
    seeds a lock of that age (None = absent); returns the tmpdir."""
    tmp = Path(tempfile.mkdtemp(prefix="cg-test-state-"))
    cglib.LOCK = tmp / "session.lock"
    cglib.LAST_ERROR = tmp / "last_error"
    if lock_age_s is not None:
        cglib.LOCK.write_text(lock_content)
        old = time.time() - lock_age_s
        os.utime(cglib.LOCK, (old, old))
    return tmp


@contextlib.contextmanager
def freeze_sleep():
    """time.sleep is a no-op inside the block and restored on the way out,
    however the block ends - a failed assert used to leave it stubbed."""
    real = time.sleep
    time.sleep = lambda s: None
    try:
        yield
    finally:
        time.sleep = real
