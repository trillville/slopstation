"""config.json and secrets.json: the two per-machine files at paths.HOME.

Both are read at call time, never at import. The chord lane has to import on a
machine that has no config.json yet, and the doctor is what says which key is
missing; a secrets.json that is absent or malformed means no keys, which
disables the keyed lanes downstream rather than crashing anything.
"""

from __future__ import annotations

import json

from slopstation import events, paths


def load() -> dict:
    """The raw file read; current() is what runtime code calls."""
    return json.loads(paths.config_file().read_text(encoding="utf-8-sig"))


_current = None


def current() -> dict:
    """This process's config.json, read once on first call."""
    global _current
    if _current is None:
        _current = load()
    return _current


def use(cfg: dict | None) -> None:
    """Test seam: make current() answer `cfg` without touching the file."""
    global _current
    _current = cfg


REQUIRED = (
    "gamingPcMac",
    "gamingPcIp",
    "sshHost",
    "tvComPort",
    "tvGamingCmd",
    "tvIdleCmd",
    "tvOffWhenDone",
)
# Missing any of these fails the voice agent at startup, not per-wake; every
# other voice key has an inert default (config.json is per-machine: a key made
# mandatory in code is an agent that will not start after a git pull).
REQUIRED_VOICE = (
    "wakeModel",
    "wakeThreshold",
    "holdWindowS",
    "followupCarryS",
    "eotThreshold",
    "eagerEotThreshold",
    "keytermCount",
    "fuzzyTitleThreshold",
    "volumeStep",
    "volumeMax",
    "ttsVoice",
    "assistantProvider",
    "assistantModelAnthropic",
    "assistantModelOpenai",
    "assistantReasoningEffort",
    "inputs",
    "assistantWebSearch",
    "assistantSearchMaxUses",
    "location",
    "followUpAfterAnnounce",
)


def missing(cfg: dict, voice: bool = False) -> list[str]:
    """Required keys absent from cfg (top level, or its voice section)."""
    if voice:
        section = cfg.get("voice") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            return list(REQUIRED_VOICE)
        return [k for k in REQUIRED_VOICE if k not in section]
    return [k for k in REQUIRED if k not in cfg]


def secrets() -> dict:
    """secrets.json as a dict, read under whatever paths.HOME is at call time."""
    try:
        return events.load_secrets(paths.secrets_file())
    except ValueError:
        print(
            f"[config] {paths.secrets_file().name} is malformed - all keyed lanes disabled"
        )
        return {}


real_key = events.real_key
