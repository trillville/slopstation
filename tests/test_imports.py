"""Every module imports on a machine with no config.json, no secrets.json and
no devices: the lanes must come up on a fresh checkout, and a config read at
import time would fail before the doctor could say what is missing.

One fresh interpreter with SLOPSTATION_HOME pointed at an empty directory,
rather than this process: by the time this file runs, the suite has imported
most of the package already, and a cached import proves nothing.
"""

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
