"""What a follow-up session keeps from the last one: the recent turns, the
tools find_tools loaded, and the confirmation gate's open questions."""

import time
from typing import Any

# Cross-session context: the last turns, and the tools find_tools loaded,
# so a follow-up session keeps what the last one found.
CARRY: dict[str, Any] = {"messages": [], "loaded": [], "gate": None, "t": 0.0}


def _trim_carry(messages):
    """Keep only complete tool exchanges beginning with a user message."""
    msgs = list(messages)
    while msgs and msgs[0].get("role") != "user":
        msgs.pop(0)
    if msgs and msgs[-1].get("tool_calls"):
        msgs.pop()
    return msgs


def load(window_s):
    """(messages, loaded tools, gate) from the last session when it ended
    within `window_s` seconds, else empty ones and no gate."""
    if time.time() - CARRY["t"] >= window_s:
        return [], [], None
    return list(CARRY["messages"]), list(CARRY["loaded"]), CARRY["gate"]


def save(messages, toolkit):
    """Keep the last complete turns and what the toolkit loaded."""
    CARRY["messages"] = _trim_carry(messages[-8:])
    CARRY["loaded"] = list(toolkit.loaded) if toolkit else []
    CARRY["gate"] = toolkit.ctx.gate if toolkit else None
    CARRY["t"] = time.time()
