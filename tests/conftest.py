"""Suite-wide setup.

The environment label is set before anything imports the package: `env` is a
record attribute alerts select on, and events.py reads it once at import, so a
test module imported first would emit records labelled prod. pytest_configure
runs after this file is imported and before any test module is collected,
which is the window that needs.

Log and state paths go to tempdirs so no test writes beside a live lane,
whether or not it remembered to ask.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_MISSING = object()


def pytest_configure(config):
    _configure()


def _configure():
    os.environ.setdefault("SLOPSTATION_ENV", "test")
    import helpers
    from slopstation import config, sessionlock

    # Before any module that derives its state paths from sessionlock.STATE is
    # imported.
    sessionlock.STATE = Path(tempfile.mkdtemp(prefix="slopstation-test-state-"))
    sessionlock.LOCK = sessionlock.STATE / "session.lock"
    sessionlock.LAST_ERROR = sessionlock.STATE / "last_error"
    sessionlock.CANCEL = sessionlock.STATE / "cancel"
    config.use(json.loads(json.dumps(helpers.CONFIG)))


# The suite predates pytest: tests rebind module attributes directly - state
# files, module functions, sentry's _on flag - rather than through monkeypatch,
# and each file used to run in its own interpreter. Snapshotting the package
# wholesale is the one restore that does not depend on every test remembering
# to clean up; files can move onto monkeypatch individually.
@pytest.fixture(autouse=True)
def _isolate():
    """Restore every slopstation module attribute the test changed, and clear
    the ambient correlation context - `turn` and `session` are inherited, so
    one test's ids would show up in the next one's events."""
    from slopstation import events

    mods = [
        m
        for name, m in list(sys.modules.items())
        if name == "slopstation" or name.startswith("slopstation.")
    ]
    # Plus the stdlib functions the suite fakes: `doctor.subprocess` is the
    # global module, so patching .run through it patches it for every later
    # test.
    mods += [time, subprocess]
    saved = [(m, dict(vars(m))) for m in mods]
    token = events._ctx.set({})
    # A fresh log directory per test: doctor's "has anything been written
    # today" row reads an empty one. _last_day with it, or the rollover guard
    # skips the mkdir and the new directory is never created.
    events.LOG_DIR = Path(tempfile.mkdtemp(prefix="slopstation-test-logs-"))
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
