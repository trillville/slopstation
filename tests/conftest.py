"""Suite-wide setup, applied before any test module is imported.

CG_ENV first and at module scope: `env` is a log attribute alerts select on,
and events.ENV is read once at import, so anything that imports slopstation
before this runs would emit test events labelled prod. pytest imports conftest
ahead of the test modules, which is what makes that ordering hold - the old
suite sniffed argv[0] for the same reason and only worked when run as scripts.

Log and state paths go to tempdirs so no test writes beside a live lane,
whether or not it remembered to ask.
"""
import os

os.environ.setdefault("CG_ENV", "test")

import json          # noqa: E402
import subprocess    # noqa: E402
import sys           # noqa: E402
import time          # noqa: E402
import tempfile      # noqa: E402
from pathlib import Path  # noqa: E402

import pytest        # noqa: E402

from slopstation import cglib, events  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

events.LOG_DIR = Path(tempfile.mkdtemp(prefix="cg-test-logs-"))
# Before any module that derives its state paths from cglib.STATE is imported.
cglib.STATE = Path(tempfile.mkdtemp(prefix="cg-test-state-"))
cglib.LOCK = cglib.STATE / "session.lock"
cglib.LAST_ERROR = cglib.STATE / "last_error"
cglib.CANCEL = cglib.STATE / "cancel"

# config.example.json is the fixture: a test that needs specific values calls
# cglib.use_config(...) itself.
CONFIG = json.loads((REPO / "config.example.json").read_text(encoding="utf-8-sig"))
cglib.use_config(json.loads(json.dumps(CONFIG)))


# The old runner gave every test file its own interpreter, so a module-level
# rebind could not outlive it. pytest shares one process, and the suite patches
# freely - state files, module functions, sentry's _on flag - so without this a
# test inherits whatever the previous one left behind. Snapshotting the package
# wholesale is blunt, but it is the only version that does not depend on every
# test remembering to clean up; tightening individual files onto pytest's
# monkeypatch fixture can happen file by file.
@pytest.fixture(autouse=True)
def _isolate():
    """Restore every slopstation module attribute the test changed, and clear
    the ambient correlation context - `turn` and `session` are inherited, so
    one test's ids would show up in the next one's events."""
    mods = [m for name, m in list(sys.modules.items())
            if name == "slopstation" or name.startswith("slopstation.")]
    # Plus the stdlib functions the suite fakes. `doctor.subprocess` is the
    # global module, so patching .run through it patches it for every later
    # test - and a test that fails before its restore leaves the fake in
    # place, which is how one red test used to turn into five.
    mods += [time, subprocess]
    saved = [(m, dict(vars(m))) for m in mods]
    token = events._ctx.set({})
    # A fresh log directory per test, because the old runner gave every file a
    # fresh process and doctor's "has anything been written today" row reads an
    # empty one. _last_day with it: the rollover guard skips LOG_DIR.mkdir when
    # the day has not changed, so the new directory would never be created.
    events.LOG_DIR = Path(tempfile.mkdtemp(prefix="cg-test-logs-"))
    events._last_day = None
    try:
        yield
    finally:
        events._ctx.reset(token)
        for mod, snapshot in saved:
            live = vars(mod)
            # Restore what changed; leave what the test ADDED, since a lazily
            # imported submodule binds itself onto its parent package here and
            # removing that would break the next import of it.
            for key, value in snapshot.items():
                if live.get(key, _MISSING) is not value:
                    live[key] = value


_MISSING = object()
