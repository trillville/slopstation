"""Blind test: _trim_carry trims the carried slice to whole exchanges starting
at a plain user turn - a tool_result without its tool_use is a 400 from
Anthropic. Run:
    .venv\\Scripts\\python tests\\test_carry.py
"""

import _bootstrap  # noqa: F401

from session_runtime import _trim_carry

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
    # Orphaned tool result at the head -> trimmed away.
    m = _trim_carry([TOOL_RES, A, U, A])
    assert first_is_plain_user(m), m

    # Assistant tool_call with no result at the tail -> dropped.
    m = _trim_carry([U, A, U, A_TOOL])
    assert no_dangling_tail(m) and first_is_plain_user(m), m

    whole = [U, A_TOOL, TOOL_RES, A]
    assert _trim_carry(whole) == whole

    # All-orphan slice collapses to empty, not a crash.
    assert _trim_carry([TOOL_RES, A_TOOL]) == []
    assert _trim_carry([]) == []

    # Two tool turns + a plain turn, sliced to 8.
    convo = [U, A_TOOL, TOOL_RES, A, U, A_TOOL, TOOL_RES, A, U, A]
    m = _trim_carry(convo[-8:])
    assert first_is_plain_user(m) and no_dangling_tail(m), m
    print("OK - _trim_carry: orphan-head dropped, dangling-tail dropped, "
          "whole exchanges preserved, empties safe")


if __name__ == "__main__":
    main()
