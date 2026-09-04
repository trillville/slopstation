"""Test package imports in a fresh process without local configuration."""

import os
import subprocess
import sys

import helpers

MODULES = [helpers.modname(p) for p in helpers.package_modules()]


def test_imports_without_config(tmp_path):
    code = "import importlib\n" + "".join(
        f"importlib.import_module({name!r})\n" for name in MODULES
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "SLOPSTATION_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stderr[-3000:]
