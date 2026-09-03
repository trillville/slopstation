"""Shared test helpers: the repository layout, the config fixture, the machine
gates, and the state-directory reset."""

import functools
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from slopstation import cglib

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "slopstation"

# config.example.json as a dict; a test that needs specific values calls
# cglib.use_config(...) itself.
CONFIG = json.loads((REPO / "config.example.json").read_text(encoding="utf-8-sig"))


def package_modules():
    """Every .py in the package."""
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def modname(path):
    """src/slopstation/agent/tools/library.py -> slopstation.agent.tools.library"""
    rel = path.relative_to(PACKAGE.parent).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


@functools.cache
def _present():
    """What this machine can actually run: steam (a local install - the gaming
    PC) and audio (real devices - the K15, opt-in because the mic is shared
    with a live lane). Detected here rather than passed in, so `pytest` on its
    own does the right thing on either box. SLOPSTATION_TEST_HAS overrides it."""
    override = os.environ.get("SLOPSTATION_TEST_HAS")
    if override is not None:
        return frozenset(n for n in override.split(",") if n)
    found = set()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"):
            found.add("steam")
    except (ImportError, OSError):
        pass
    if os.environ.get("SLOPSTATION_TEST_AUDIO"):
        found.add("audio")
    return frozenset(found)


def wants(*needs):
    """Skip unless this machine has every need."""
    missing = [n for n in needs if n not in _present()]
    if missing:
        pytest.skip(f"needs {', '.join(missing)}")


def fresh_state(lock_age_s=None, lock_content="x"):
    """Point every state-file constant into a new tempdir. lock_age_s seeds a
    session lock of that age (None = absent). Returns the tempdir."""
    from slopstation.agent.telemetry import traces
    from slopstation.agent.tools import library, operations, steamstore

    tmp = Path(tempfile.mkdtemp(prefix="slopstation-test-state-"))
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
