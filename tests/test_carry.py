"""_trim_carry trims the carried slice to whole exchanges starting
at a plain user turn - a tool_result without its tool_use is a 400 from
Anthropic.
"""

from slopstation.agent.speech.session_runtime import _trim_carry

U = {"role": "user", "content": "what racing games do i have"}
A = {"role": "assistant", "content": "Three."}
A_TOOL = {
    "role": "assistant",
    "content": [
        {"type": "tool_use", "id": "t1", "name": "launch_game", "input": {"appid": 1}}
    ],
}
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


def test_orphaned_tool_result_at_the_head_is_trimmed():
    m = _trim_carry([TOOL_RES, A, U, A])
    assert first_is_plain_user(m), m


def test_dangling_tool_use_at_the_tail_is_dropped():
    # Assistant tool_call with no result at the tail -> dropped.
    m = _trim_carry([U, A, U, A_TOOL])
    assert no_dangling_tail(m) and first_is_plain_user(m), m


def test_whole_exchanges_pass_through_unchanged():
    whole = [U, A_TOOL, TOOL_RES, A]
    assert _trim_carry(whole) == whole


def test_all_orphan_slice_collapses_to_empty():
    # All-orphan slice collapses to empty, not a crash.
    assert _trim_carry([TOOL_RES, A_TOOL]) == []
    assert _trim_carry([]) == []


def test_sliced_window_of_tool_turns_is_whole():
    # Two tool turns + a plain turn, sliced to 8.
    convo = [U, A_TOOL, TOOL_RES, A, U, A_TOOL, TOOL_RES, A, U, A]
    m = _trim_carry(convo[-8:])
    assert first_is_plain_user(m) and no_dangling_tail(m), m
