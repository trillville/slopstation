"""Test the shared confirmation gate for destructive tools."""

from slopstation.agent.llm import confirm


def test_gate_asks_once_then_commits_on_a_later_turn():
    clock = {"t": 1000.0}
    gate = confirm.ConfirmGate(now=lambda: clock["t"])
    scope = ("series", 81189, (2,), False)
    # First call: asks, does not act.
    assert gate.confirmed(scope, "aa0001") is False and gate.pending(scope)
    # Same turn again: the model cannot answer its own question.
    assert gate.confirmed(scope, "aa0001") is False
    # A later turn inside the lifetime: commits. The ask stays armed until
    # the tool reports success, so a failed attempt can retry without asking.
    clock["t"] += 30
    assert gate.confirmed(scope, "aa0002") is True and gate.pending(scope)
    assert gate.confirmed(scope, "aa0003") is True
    gate.done(scope)
    assert not gate.pending(scope)
    # Spent means the next call asks afresh rather than acting twice.
    assert gate.confirmed(scope, "aa0004") is False
    # No turn id can never confirm: fail closed.
    assert gate.confirmed(scope, None) is False
    # Another scope is its own question.
    assert gate.confirmed(("movie", 1, (), False), "aa0003") is False


def test_a_stale_ask_is_asked_again_not_honoured():
    clock = {"t": 1000.0}
    gate = confirm.ConfirmGate(now=lambda: clock["t"])
    scope = ("movie", 438631, (), False)
    assert gate.confirmed(scope, "bb0001") is False
    clock["t"] += confirm.ASK_TTL_S + 1
    # The user walked away: a yes ten minutes later re-asks instead of acting,
    # and the stale record is gone rather than kept forever.
    assert not gate.pending(scope)
    assert gate.confirmed(scope, "bb0002") is False
    clock["t"] += 1
    assert gate.confirmed(scope, "bb0003") is True
