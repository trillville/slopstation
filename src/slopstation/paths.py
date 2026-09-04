"""Paths for configuration, state, and logs.

SLOPSTATION_HOME can override the checkout root used by default.
"""

import os
import pathlib

# src/slopstation/paths.py -> the checkout root.
_CHECKOUT = pathlib.Path(__file__).resolve().parents[2]

HOME = pathlib.Path(os.environ.get("SLOPSTATION_HOME") or _CHECKOUT)


def state(name: str = "") -> pathlib.Path:
    """state/, or one file in it."""
    return HOME / "state" / name


def logs() -> pathlib.Path:
    return HOME / "logs"


def config_file() -> pathlib.Path:
    return HOME / "config.json"


def secrets_file() -> pathlib.Path:
    return HOME / "secrets.json"


def couch_log() -> pathlib.Path:
    return HOME / "couch.log"
