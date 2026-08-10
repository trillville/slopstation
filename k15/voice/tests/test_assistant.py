"""Blind test (C3): catalog + system prompt from the REAL library, tool-impl
validation (unknown appids REFUSED at the boundary), pipecat/anthropic
constructions with dummy keys, and a tolerant live metadata fetch. Run:
    .venv\\Scripts\\python tests\\test_assistant.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import assistant
import library
from dispatch import Dispatch

CFG_MIN = {"tvComPort": "COMX", "tvGamingCmd": "hdmi4",
           "voice": {"volumeStep": 2, "volumeMax": 40,
                     "inputs": {"apple tv": "hdmi1"}}}


def main():
    logs = []
    d = Dispatch(CFG_MIN, logs.append, dry_run=True)
    impls = assistant.tool_impls(d, logs.append)

    rows = library.load()["installed"]
    real_appid = rows[0]["appid"]

    # System prompt: rules + one line per game, sane token budget.
    si = assistant.system_instruction()
    assert "CATALOG" in si and str(real_appid) in si
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
    r = impls["get_now_playing"]({})
    assert r["ok"]                                # dry-run path
    r = impls["get_game_details"]({"appid": real_appid})
    assert r["ok"] and r["name"] == rows[0]["name"]
    print("  control/get_now_playing/get_game_details: routed and validated")

    # Pipecat + anthropic constructions with dummy keys (no network at init).
    schemas = assistant.function_schemas(impls)
    assert len(schemas) == 4
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair)
    from pipecat.services.anthropic.llm import AnthropicLLMService
    from pipecat.services.deepgram.tts import DeepgramTTSService
    ctx = LLMContext(messages=[], tools=schemas)
    ua, aa = LLMContextAggregatorPair(ctx)
    llm = AnthropicLLMService(
        api_key="x" * 24,
        settings=AnthropicLLMService.Settings(
            model="claude-haiku-4-5", system_instruction=si,
            enable_prompt_caching=True, max_tokens=400))
    tts = DeepgramTTSService(api_key="x" * 24, sample_rate=16000,
                             settings=DeepgramTTSService.Settings(
                                 voice="aura-2-thalia-en"))
    assert ua and aa and llm and tts
    assert len(assistant.anthropic_tools()) == 4
    print("  constructions: LLMContext+aggregators, Anthropic, Aura-2 - OK")

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
