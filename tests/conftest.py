"""Suite-wide setup: the environment label, the config fixture, and a fresh
runtime home per test."""

import copy
import os

import pytest


def pytest_configure(config):
    # Before anything imports the package: events.py reads the label once at
    # import, and `env` is a record attribute alerts select on.
    os.environ.setdefault("SLOPSTATION_ENV", "test")
    import helpers
    from slopstation import config as cfg

    cfg._current = copy.deepcopy(helpers.CONFIG)


@pytest.fixture(autouse=True)
def _fresh_home(tmp_path):
    """A fresh runtime home per test - state, logs and markers move with
    paths.HOME - and a clean correlation context, since `turn` and `session`
    would otherwise carry into the next test's events."""
    from slopstation import events, paths

    paths.HOME = tmp_path
    paths.state().mkdir()
    events._last_day = None  # or the new home never gets its log directory
    token = events._ctx.set({})
    try:
        yield
    finally:
        events._ctx.reset(token)
