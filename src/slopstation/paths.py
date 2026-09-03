"""Where the runtime data lives - config.json, secrets.json, state/, logs/,
couch.log, media/.env - and nothing else.

HOME is the checkout by default, because the install is editable, and
SLOPSTATION_HOME overrides it. Everything below reads HOME when called, never
at import, so re-pointing HOME moves the whole tree: the test suite gives every
test a fresh one, and a deploy that ran from a runner workspace would still
find the live checkout's lock.
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
