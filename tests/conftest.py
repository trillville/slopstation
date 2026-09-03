"""Suite-wide setup.

The environment label is set before anything imports the package: `env` is a
record attribute alerts select on, and events.py reads it once at import, so a
test module imported first would emit records labelled prod. pytest_configure
runs after this file is imported and before any test module is collected,
which is the window that needs.

Every test gets a fresh runtime home (paths.HOME): state, logs and markers
move with it, so no test writes beside a live lane or sees another's files.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    _configure()


def _configure():
    os.environ.setdefault("SLOPSTATION_ENV", "test")
    import helpers
    from slopstation import config, paths

    paths.HOME = Path(tempfile.mkdtemp(prefix="slopstation-test-home-"))
    config.use(json.loads(json.dumps(helpers.CONFIG)))


@pytest.fixture(autouse=True)
def _fresh_home(tmp_path):
    """A fresh runtime home per test, and a clean correlation context: `turn`
    and `session` are inherited, so one test's ids would show up in the next
    one's events."""
    from slopstation import events, paths

    paths.HOME = tmp_path
    paths.state().mkdir()
    # The rollover guard skips the log directory's mkdir when the day has not
    # changed, so the new home would never get one.
    events._last_day = None
    token = events._ctx.set({})
    try:
        yield
    finally:
        events._ctx.reset(token)
