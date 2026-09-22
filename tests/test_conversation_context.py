"""Test trimming carried context to complete exchanges."""

from slopstation.agent.speech.carry import _trim_carry

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


def test_a_follow_up_inside_the_window_gets_the_last_session_back(monkeypatch):
    """What save() keeps, load() returns inside the window and drops outside it."""
    import types

    from slopstation.agent.speech import carry

    monkeypatch.setattr(
        carry, "CARRY", {"messages": [], "loaded": [], "gate": None, "t": 0.0}
    )
    gate = object()
    toolkit = types.SimpleNamespace(
        loaded=["delete_media"], ctx=types.SimpleNamespace(gate=gate)
    )
    carry.save([{"role": "user", "content": "hi"}], toolkit)
    assert carry.load(60) == (
        [{"role": "user", "content": "hi"}],
        ["delete_media"],
        gate,
    )
    monkeypatch.setitem(carry.CARRY, "t", 0.0)
    assert carry.load(60) == ([], [], None)
