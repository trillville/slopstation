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
            # A paged tool lists rows: it takes a limit and says so.
            assert "limit" in spec.properties, f"{spec.name} is paged without a limit"
            assert "count" in spec.description, spec.name
    destructive = {s.name for s in assistant.REGISTRY if s.risk == "destructive"}
    assert destructive == {"delete_media"}
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
        media=object(),
        steam=object(),
        voice={"steamDataTools": True},
    )
    assert set(full) == set(assistant.REGISTRY.names())
    # Absent services drop exactly the tools that need them.
    bare = assistant.tool_impls(dispatch, log)
    dropped = set(assistant.REGISTRY.names()) - set(bare)
    assert dropped == {
        s.name for s in assistant.REGISTRY if {"operations", "media"} & set(s.needs)
    }
    off = assistant.tool_impls(dispatch, log, voice={"steamDataTools": False})
    assert set(bare) - set(off) == {"list_games", "search_store"}


def test_renders_follow_the_registry_order_and_filter():
    names = assistant.REGISTRY.names()
    assert [t["name"] for t in assistant.anthropic_tools()] == names
    assert [t["name"] for t in assistant.openai_tools()] == names
    some = {"volume", "nav"}
    assert {t["name"] for t in assistant.anthropic_tools(some)} == some
    (vol,) = assistant.openai_tools({"volume"})
    assert vol["type"] == "function" and "function" not in vol
    assert vol["parameters"]["required"] == ["action"]
