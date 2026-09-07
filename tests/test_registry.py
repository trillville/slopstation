"""Test the tool registry: its invariants, its gating, and its renders."""

import types

import pytest

from helpers import CapturingLog
from slopstation.agent.llm import assistant, registry, toolsets


def test_every_spec_is_well_formed():
    # The registry constructor rejects duplicates, unknown risks and areas;
    # this holds the softer rules a new tool is likeliest to skip.
    for spec in assistant.REGISTRY:
        assert len(spec.keywords) >= 3, f"{spec.name} needs keywords for find_tools"
        assert spec.description.strip(), spec.name
        assert set(spec.required) <= set(spec.properties), spec.name
        if spec.paged:
            # A paged tool lists rows: it takes a limit and an offset, and
            # says it returns the count, so "the rest" is one more call.
            assert {"limit", "offset"} <= set(spec.properties), spec.name
            assert "count" in spec.description, spec.name
        if spec.busy:
            # Spoken while the user waits: a few words, and {game} only where
            # the tool takes an appid to fill it from.
            assert len(spec.busy.split()) <= 4, f"{spec.name}: long busy phrase"
            assert spec.busy == spec.busy.strip() and not spec.busy.endswith(".")
            if "{game}" in spec.busy:
                assert "appid" in spec.properties, spec.name
    destructive = {s.name for s in assistant.REGISTRY if s.risk == "destructive"}
    assert destructive == {
        "delete_media",
        "delete_torrent",
        "delete_path",
        "resolve_queue_item",
        "uninstall_game",
    }
    # Destructive tools are never in the default set: the search step is a
    # natural pause before them.
    assert all(not assistant.REGISTRY.get(n).default for n in destructive)


def test_constructor_refuses_a_bad_spec():
    ok = toolsets.rig.SPECS[0]
    with pytest.raises(ValueError, match="duplicate"):
        registry.Registry([ok, ok])
    for field, value, why in (
        ("risk", "nuclear", "risk"),
        ("area", "attic", "area"),
        ("needs", ("teleporter",), "needs"),
    ):
        bad = types.SimpleNamespace(**{**ok.__dict__, field: value})
        with pytest.raises(ValueError, match=why):
            registry.Registry([bad])


def test_every_offered_spec_has_an_implementation():
    # With every service present, each spec's name is callable, and nothing
    # is callable that has no spec. A tool added to one side only fails here.
    log = CapturingLog()
    dispatch = types.SimpleNamespace(dry_run=True, utterance=None)
    full = assistant.tool_impls(
        dispatch,
        log,
        operations=object(),
        media=types.SimpleNamespace(qbit=object(), prowlarr=object()),
        steam=object(),
        voice={"steamDataTools": True},
    )
    assert set(full) == set(assistant.REGISTRY.names())
    # Absent services drop exactly the tools that need them.
    bare = assistant.tool_impls(dispatch, log)
    dropped = set(assistant.REGISTRY.names()) - set(bare)
    absent = {"operations", "media", "torrents", "prowlarr", "steam_account"}
    assert dropped == {s.name for s in assistant.REGISTRY if absent & set(s.needs)}
    off = assistant.tool_impls(dispatch, log, voice={"steamDataTools": False})
    # The kill switch drops exactly the tools that need the data lane.
    assert set(bare) - set(off) == {
        s.name for s in assistant.REGISTRY if "steam_data" in s.needs
    }
    assert {"list_games", "search_store", "steam_api", "friends"} <= set(bare) - set(
        off
    )


def test_renders_follow_the_registry_order_and_filter():
    names = assistant.REGISTRY.names()
    assert [t["name"] for t in assistant.anthropic_tools()] == names
    assert [t["name"] for t in assistant.openai_tools()] == names
    some = {"volume", "nav"}
    assert {t["name"] for t in assistant.anthropic_tools(some)} == some
    (vol,) = assistant.openai_tools({"volume"})
    assert vol["type"] == "function" and "function" not in vol
    assert vol["parameters"]["required"] == ["action"]


def test_bindings_hold_spec_and_function_together_and_gate_the_destructive():
    ctx = registry.ToolContext(
        dispatch=types.SimpleNamespace(
            dry_run=False, utterance=types.SimpleNamespace(turn="t1")
        ),
        log=CapturingLog("voice"),
    )
    volume, delete_path = (
        assistant.REGISTRY.get("volume"),
        assistant.REGISTRY.get("delete_path"),
    )
    bind = registry.Bindings(ctx, [volume, delete_path])
    # The function's name is the tool's name: nothing else is accepted.
    with pytest.raises(ValueError, match="no ToolSpec"):

        @bind
        def nobody(args):
            return {}

    # A destructive spec cannot be bound as a plain function, nor a plain
    # spec as a destructive one.
    with pytest.raises(ValueError, match="bind.destructive"):

        @bind
        def delete_path(args):
            return {}

    with pytest.raises(ValueError, match="@bind"):

        @bind.destructive
        def volume(args):
            return {}

    # A spec left unbound fails the toolset, not a later call.
    with pytest.raises(ValueError, match="no implementation"):
        bind.impls()

    def volume(args):  # noqa: F811
        return {"ok": True}

    bind(volume)
    acted = []

    def delete_path(args):  # noqa: F811
        if args.get("bad"):
            return {"ok": False, "error": "no"}
        return registry.Plan(
            ("path", "x"),
            "Delete x?",
            lambda: acted.append(1) or {"ok": True},
            "delete x",
        )

    bind.destructive(delete_path)
    impls = bind.impls()
    assert set(impls) == {"volume", "delete_path"}
    with pytest.raises(ValueError, match="bound twice"):
        bind(volume)
    # An error passes through; a Plan is asked, refused in its own turn, run
    # on a later one, and then spent.
    assert impls["delete_path"]({"bad": True}) == {"ok": False, "error": "no"}
    assert impls["delete_path"]({}) == {"ok": False, "acknowledgment": "Delete x?"}
    assert not impls["delete_path"]({})["ok"] and not acted
    ctx.dispatch.utterance = types.SimpleNamespace(turn="t2")
    assert impls["delete_path"]({})["ok"] and acted == [1]
    assert not ctx.gate.pending(("path", "x"))
    # A dry run previews the plan and never asks.
    ctx.dispatch.dry_run = True
    dry = impls["delete_path"]({})
    assert dry == {"ok": True, "dry_run": True, "detail": "would delete x"}
    assert acted == [1] and not ctx.gate.pending(("path", "x"))
    # No utterance at all fails closed.
    ctx.dispatch = types.SimpleNamespace(dry_run=False, utterance=None)
    assert not impls["delete_path"]({})["ok"] and acted == [1]


def test_a_toolkit_takes_a_gate_to_carry_between_sessions():
    from slopstation.agent.llm import confirm

    gate = confirm.ConfirmGate()
    dispatch = types.SimpleNamespace(dry_run=True, utterance=None)
    assert (
        assistant.Toolkit(dispatch, CapturingLog("voice"), gate=gate).ctx.gate is gate
    )
