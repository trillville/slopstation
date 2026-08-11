"""Blind test (C3): catalog + system prompt from the REAL library, tool-impl
validation (unknown appids REFUSED at the boundary), pipecat/anthropic
constructions with dummy keys, and a tolerant live metadata fetch. Run:
    .venv\\Scripts\\python tests\\test_assistant.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import assistant
import library
from dispatch import Dispatch

CFG_MIN = {"tvComPort": "COMX", "tvGamingCmd": "hdmi4",
           "voice": {"volumeStep": 2, "volumeMax": 40,
                     "inputs": {"apple tv": "hdmi1", "gaming": "hdmi4"}}}


def main():
    logs = []
    d = Dispatch(CFG_MIN, logs.append, dry_run=True)
    impls = assistant.tool_impls(d, logs.append)

    rows = library.load()["installed"]
    real_appid = rows[0]["appid"]

    # System prompt: rules + one line per game, sane token budget.
    si = assistant.system_instruction(CFG_MIN)
    assert "CATALOG" in si and str(real_appid) in si
    # The mishear-repair rule (live: "met games" confused the model until the
    # prompt said input is STT and taught it to read phonetically).
    assert "speech-to-text" in si and "mishears" in si
    # Dynamic tail: the facts only config knows, each stated exactly once -
    # date (so "lately" means something), spoken input names + the gaming
    # input's start-a-session behavior, volume clamp, mute-is-blind.
    assert time.strftime("%Y-%m-%d") in si
    assert "apple tv" in si and "'gaming' starts a session" in si
    assert "clamped" in si and "blind toggle" in si
    # Out-of-catalog carve-out, so mishear-repair can't force a wrong match.
    assert "isn't in the library" in si
    n_tokens = len(si) // 4
    assert 500 < n_tokens < 30000, n_tokens
    print(f"  system prompt: ~{n_tokens} tokens, {len(library.catalog_lines())} games")

    # Tool boundary: unknown appid refused, real one dry-dispatches.
    r = impls["launch_game"]({"appid": 999999999})
    assert not r["ok"] and "not in the catalog" in r["error"]
    r = impls["launch_game"]({"appid": real_appid})
    assert r["ok"] and "dry-run" in r["detail"], r
    print(f"  launch_game: unknown appid refused, {real_appid} dry-dispatched")

    # control routing + clamp via dispatch.
    assert impls["control"]({"action": "volume_up"})["ok"]
    assert impls["control"]({"action": "set_volume", "level": 30})["ok"]
    assert not impls["control"]({"action": "self_destruct"})["ok"]
    r = impls["control"]({"action": "set_volume"})     # no level -> refused
    assert not r["ok"] and "level" in r["error"]
    r = impls["get_now_playing"]({})
    assert r["ok"]                                # dry-run path
    r = impls["get_game_details"]({"appid": real_appid})
    assert r["ok"] and r["name"] == rows[0]["name"] and r["installed"]
    # Owned-but-not-installed must still come back named (review gap: the
    # model got details for a game it couldn't name).
    inst_ids = {row["appid"] for row in rows}
    owned_only = [a for a, o in library.load().get("owned", {}).items()
                  if int(a) not in inst_ids and o.get("name")]
    if owned_only:
        r = impls["get_game_details"]({"appid": int(owned_only[0])})
        assert r["ok"] and r["name"] and not r["installed"], r
    print("  control/get_now_playing/get_game_details: routed and validated")

    # Both tool renderers cover every tool, in each provider's shape.
    at, ot = assistant.anthropic_tools(), assistant.openai_tools()
    names = {n for n, *_ in assistant.TOOL_DEFS}
    assert {t["name"] for t in at} == names
    # Responses API tools are FLAT (name/parameters at top level, no nesting).
    assert {t["name"] for t in ot} == names
    assert all(t["type"] == "function" and "parameters" in t and "function" not in t
               for t in ot)
    assert all("input_schema" in t for t in at)
    # Single-source-of-truth: the volume range lives in the prompt (clamped),
    # not the tool def; session-start semantics live on launch_game.
    assert "0-100" not in str(assistant.TOOL_DEFS)
    assert "never call start_session" in str(assistant.TOOL_DEFS)
    assert set(assistant.BACKENDS) == {"anthropic", "openai"}
    assert assistant.BACKENDS["anthropic"].key == "anthropicApiKey"
    assert assistant.BACKENDS["openai"].key == "openaiApiKey"
    print(f"  tool renderers: {len(at)} anthropic + {len(ot)} openai, both cover all")

    # Pipecat constructions with dummy keys (no network at init), both
    # providers - built through the PRODUCTION _make_llm, not a test-local
    # copy: the copy passed a dict for `reasoning` while the dataclass
    # settings accepted it silently, and the crash only surfaced at live
    # inference ("'dict' object has no attribute 'model_dump'", 2026-08-11).
    schemas = assistant.function_schemas(impls)
    assert len(schemas) == 4
    import voice_agent
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair)
    from pipecat.services.deepgram.tts import DeepgramTTSService
    ctx = LLMContext(messages=[], tools=schemas)
    ua, aa = LLMContextAggregatorPair(ctx)
    dummy = {"anthropicApiKey": "x" * 24, "openaiApiKey": "x" * 24}
    voice_a = {**CFG_MIN["voice"], "assistantProvider": "anthropic",
               "assistantModel": "claude-haiku-4-5"}
    llm_a = voice_agent._make_llm(voice_a, dummy, si)
    voice_o = {**voice_a, "assistantProvider": "openai",
               "assistantModelOpenai": "gpt-5.6-luna",
               "assistantReasoningEffort": "low"}
    llm_o = voice_agent._make_llm(voice_o, dummy, si)
    # Prove the inference path's model_dump() call survives - exactly the
    # line that died live when reasoning was a plain dict.
    assert llm_o._settings.reasoning.model_dump(exclude_none=True) == {
        "effort": "low"}, llm_o._settings.reasoning
    tts = DeepgramTTSService(api_key="x" * 24, sample_rate=16000,
                             settings=DeepgramTTSService.Settings(
                                 voice="aura-2-thalia-en"))
    assert ua and aa and llm_a and llm_o and tts
    # OpenAIBackend must default to a REAL reasoning effort, not disable it.
    import inspect
    eff = inspect.signature(assistant.OpenAIBackend.__init__).parameters["effort"]
    assert eff.default not in (None, "none"), f"effort defaults to {eff.default!r}"
    print("  constructions: LLMContext, Anthropic + OpenAI Responses, Aura-2 - OK")

    # Live metadata (keyless APIs) - tolerant: network may be absent.
    try:
        meta = library.fetch_meta_one(real_appid)
        assert meta.get("tags") or meta.get("genres"), meta
        print(f"  live metadata for {real_appid}: tags={meta.get('tags', [])[:3]} "
              f"controller={meta.get('controller')}")
    except Exception as e:
        print(f"  live metadata SKIPPED ({e}) - rerun with network")

    print("OK - assistant: prompt, tool boundary, routing, constructions")


if __name__ == "__main__":
    main()
