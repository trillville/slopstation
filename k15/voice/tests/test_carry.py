"""Blind test (review fix): _trim_carry must never hand the next session a
tool_result without its tool_use (Anthropic 400s on that). It trims the
carried slice to whole exchanges starting at a plain user turn. Run:
    .venv\\Scripts\\python tests\\test_carry.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from voice_agent import _trim_carry

U = {"role": "user", "content": "what racing games do i have"}
A = {"role": "assistant", "content": "Three."}
A_TOOL = {"role": "assistant",
          "content": [{"type": "tool_use", "id": "t1", "name": "launch_game",
                       "input": {"appid": 1}}]}
TOOL_RES = {"role": "tool", "tool_call_id": "t1", "content": "ok"}


def first_is_plain_user(msgs):
    return bool(msgs) and msgs[0]["role"] == "user" and "tool_call_id" not in msgs[0]


def no_dangling_tail(msgs):
    if not msgs:
        return True
    last = msgs[-1]
    if last["role"] != "assistant" or not isinstance(last["content"], list):
        return True
    return not any(b.get("type") == "tool_use" for b in last["content"])


def main():
    # Slice starting on an orphaned tool result -> front-trimmed away.
    m = _trim_carry([TOOL_RES, A, U, A])
    assert first_is_plain_user(m), m

    # Slice ending on an assistant tool_call with no result -> tail dropped.
    m = _trim_carry([U, A, U, A_TOOL])
    assert no_dangling_tail(m) and first_is_plain_user(m), m

    # A clean whole exchange is preserved intact.
    whole = [U, A_TOOL, TOOL_RES, A]
    assert _trim_carry(whole) == whole

    # Degenerate: all-orphan slice collapses to empty (safe, not a crash).
    assert _trim_carry([TOOL_RES, A_TOOL]) == []
    assert _trim_carry([]) == []

    # Real shape: two tool turns + a plain "thanks" turn, sliced to 8.
    convo = [U, A_TOOL, TOOL_RES, A, U, A_TOOL, TOOL_RES, A, U, A]
    m = _trim_carry(convo[-8:])
    assert first_is_plain_user(m) and no_dangling_tail(m), m
    print("OK - _trim_carry: orphan-head dropped, dangling-tail dropped, "
          "whole exchanges preserved, empties safe")


if __name__ == "__main__":
    main()
