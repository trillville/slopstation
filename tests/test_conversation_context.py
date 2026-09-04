"""Test trimming carried context to complete exchanges."""

from slopstation.agent.speech.session import _trim_carry

U = {"role": "user", "content": "what racing games do i have"}
A = {"role": "assistant", "content": "Three."}
A_TOOL = {
    "role": "assistant",
    "tool_calls": [
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "launch_game", "arguments": '{"appid": 1}'},
        }
    ],
}
TOOL_RES = {"role": "tool", "tool_call_id": "t1", "content": "ok"}


def test_orphaned_tool_result_at_the_head_is_trimmed():
    assert _trim_carry([TOOL_RES, A, U, A]) == [U, A]


def test_dangling_tool_call_at_the_tail_is_dropped():
    assert _trim_carry([U, A, U, A_TOOL]) == [U, A, U]


def test_whole_exchanges_pass_through_unchanged():
    whole = [U, A_TOOL, TOOL_RES, A]
    assert _trim_carry(whole) == whole


def test_all_orphan_slice_collapses_to_empty():
    assert _trim_carry([TOOL_RES, A_TOOL]) == []
    assert _trim_carry([]) == []


def test_sliced_window_of_tool_turns_is_whole():
    convo = [U, A_TOOL, TOOL_RES, A, U, A_TOOL, TOOL_RES, A, U, A]
    assert _trim_carry(convo[-8:]) == [U, A_TOOL, TOOL_RES, A, U, A]
