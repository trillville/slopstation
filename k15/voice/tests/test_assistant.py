"""Blind test: catalog + system prompt from the REAL library, tool-impl
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
import cglib
import library
from dispatch import Dispatch

CFG_MIN = {"tvComPort": "COMX", "tvGamingCmd": "hdmi4",
           "voice": {"volumeStep": 2, "volumeMax": 40,
                     "assistantWebSearch": False, "assistantSearchMaxUses": 2,
                     "location": {"city": "", "region": "", "country": "",
                                  "timezone": ""},
                     "inputs": {"apple tv": "hdmi1", "gaming": "hdmi4"}}}


def main():
    log = cglib.CapturingLog("assistant")
    d = Dispatch(CFG_MIN, log, dry_run=True)
    impls = assistant.tool_impls(d, log)

    rows = library.load()["installed"]
    real_appid = rows[0]["appid"]

    si = assistant.system_instruction(CFG_MIN)
    assert "CATALOG" in si and str(real_appid) in si
    # Mishear-repair: the model must know its input is STT, not typed text.
    assert "speech-to-text" in si and "mishears" in si
    # Dynamic tail: the facts only config knows, each stated exactly once -
    # the date (so "lately" resolves), input names, volume clamp, mute-is-blind.
    assert time.strftime("%Y-%m-%d") in si
    # A date with no zone is half a fact - the model closed the gap toward UTC
    # and dated briefs tomorrow. CFG_MIN ships an empty location, which is a
    # real deployment shape, so it must still say the day is local.
    assert "local time" in si
    zoned = {**CFG_MIN["voice"],
             "location": {**CFG_MIN["voice"]["location"],
                          "timezone": "America/Los_Angeles"}}
    si_tz = assistant.system_instruction({**CFG_MIN, "voice": zoned})
    assert f"{time.strftime('%Y-%m-%d')} in America/Los_Angeles" in si_tz
    assert "apple tv" in si and "'gaming' starts a session" in si
    assert "clamped" in si and "blind toggle" in si
    # Out-of-catalog carve-out, so mishear-repair can't force a wrong match.
    assert "isn't in the library" in si
    n_tokens = len(si) // 4
    assert 500 < n_tokens < 30000, n_tokens
    print(f"  system prompt: ~{n_tokens} tokens, {len(library.catalog_lines())} games")

    r = impls["launch_game"]({"appid": 999999999})
    assert not r["ok"] and "not in the catalog" in r["error"]
    r = impls["launch_game"]({"appid": real_appid})
    assert r["ok"] and "dry-run" in r["detail"], r
    print(f"  launch_game: unknown appid refused, {real_appid} dry-dispatched")

    assert impls["control"]({"action": "volume_up"})["ok"]
    assert impls["control"]({"action": "set_volume", "level": 30})["ok"]
    assert not impls["control"]({"action": "self_destruct"})["ok"]
    r = impls["control"]({"action": "set_volume"})     # no level -> refused
    assert not r["ok"] and "level" in r["error"]
    # stop_listening with nothing to stop (the REPL, the probes): refused
    # truthfully rather than reporting a mic it never closed.
    r = impls["stop_listening"]({})
    assert not r["ok"] and "nothing is listening" in r["error"], r
    stops = []
    simpls = assistant.tool_impls(d, log,
                                  on_stop_listening=lambda: stops.append(1))
    r = simpls["stop_listening"]({})
    assert r["ok"] and stops == [1], (r, stops)
    r = impls["get_now_playing"]({})
    assert r["ok"]                                # dry-run path
    r = impls["get_game_details"]({"appid": real_appid})
    assert r["ok"] and r["name"] == rows[0]["name"] and r["installed"]
    # background_task without a JobStore (REPL, or the CLI missing): refused.
    r = impls["background_task"]({"task": "find coop deals"})
    assert not r["ok"] and "aren't available" in r["error"]

    class FakeJobs:
        def enqueue(self, task, asked=None):
            self.asked = asked
            return True, "queued - the result will be announced"
    fake = FakeJobs()
    jimpls = assistant.tool_impls(d, log, jobs=fake)
    d.begin_utterance("4c1d0e", "find me co-op deals")   # what the gate writes
    r = jimpls["background_task"]({"task": "find coop deals"})
    assert r["ok"] and "queued" in r["detail"]
    # The user's words ride the utterance snapshot into the job record - the
    # tool call runs in a task whose ambient context predates the utterance.
    assert fake.asked == "find me co-op deals", fake.asked
    assert not jimpls["background_task"]({"task": "  "})["ok"]
    # Owned-but-not-installed must still come back named.
    inst_ids = {row["appid"] for row in rows}
    owned_only = [a for a, o in library.load().get("owned", {}).items()
                  if int(a) not in inst_ids and o.get("name")]
    if owned_only:
        r = impls["get_game_details"]({"appid": int(owned_only[0])})
        assert r["ok"] and r["name"] and not r["installed"], r
    print("  control/stop_listening/get_now_playing/get_game_details: "
          "routed and validated")

    # --- data lane: list_games / search_store routing + the kill switch ------
    assert "list_games" in impls and "search_store" in impls
    r = impls["list_games"]({"source": "nope"})
    assert not r["ok"] and "unknown source" in r["error"], r
    r = impls["list_games"]({"source": "downloading"})     # no account session here
    assert not r["ok"] and "enrolled" in r["error"], r
    # install_game is offered with OR without the account session: without one
    # it navigates to the game's page so the controller can finish the job.
    assert "install_game" in impls
    import types
    fake_steam = types.SimpleNamespace(available=lambda: True,
                                       install=lambda a: {"ok": True, "detail": "queued"},
                                       download_status=lambda: [])
    with_steam = assistant.tool_impls(d, log, steam=fake_steam)
    rr = with_steam["install_game"]({"appid": 999999999})
    assert not rr["ok"] and "not in the catalog" in rr["error"], rr
    r = impls["search_store"]({})                           # neither term nor tags
    assert not r["ok"] and ("term" in r["error"] or "genre" in r["error"]), r
    # steamDataTools off -> the two store tools vanish from impls AND schemas,
    # so the model stops SEEING them (selection pressure), not just calling them.
    gated = assistant.tool_impls(d, log, voice={"steamDataTools": False})
    assert "list_games" not in gated and "search_store" not in gated
    assert "quit_game" in gated and "nav" in gated   # action tools aren't gated
    # 11 tools minus the two store ones the kill switch drops.
    assert len(assistant.function_schemas(gated)) == 9, len(assistant.function_schemas(gated))
    # The facts-vs-judgment split is written into the descriptions the model reads.
    assert "not background_task" in str(assistant.TOOL_DEFS)
    print("  list_games/search_store: routed, refused cleanly, kill-switch gates")

    # --- fail-soft: an impl that RAISES must return an error, never propagate -
    # (the audit's HIGH: an expired/revoked token has available()==True, then
    # the steam call raises; the tool must answer, not break the turn.)
    class RaisingSteam:
        def available(self): return True
        def install(self, a): raise RuntimeError("token revoked")
        def download_status(self): raise RuntimeError("token revoked")
    rimpls = assistant.tool_impls(d, log, steam=RaisingSteam())
    _isn, _nav = library.installed_name, d.nav
    library.installed_name = lambda a: None      # not-installed -> reaches steam.install
    navd = []
    d.nav = lambda kind, arg=None: (navd.append((kind, arg)),
                                    types.SimpleNamespace(ok=True, detail="showing"))[1]
    inst = rimpls["install_game"]({"appid": real_appid})
    library.installed_name, d.nav = _isn, _nav
    # A DEAD token must not end the request: it falls through to the TV path.
    assert inst["ok"] and "press Install" in inst["detail"], inst
    assert navd == [("details", real_appid)], navd
    dl = rimpls["list_games"]({"source": "downloading"})
    assert not dl["ok"] and "Steam" in dl["error"], dl
    assert {"install_error", "download_status_error"} <= set(log.events())
    # And the function_schemas backstop: a handler whose impl raises still calls
    # result_callback (with an error) instead of leaving the turn hung.
    import asyncio as _a
    def boom(_): raise ValueError("kaboom")
    sch = assistant.function_schemas({"get_now_playing": boom})[0]
    got = []
    class P:
        arguments = {}
        async def result_callback(self, out): got.append(out)
    _a.run(sch.handler(P()))
    assert got and got[0]["ok"] is False, got

    # --- every tool call is RECORDED, including the ones that raise ----------
    # Neither telemetry system could say which tool ran with what args (a
    # tool-calling llm span traces as output:null), so function_schemas is the
    # one home that emits it. A raising impl must still be recorded - that is
    # exactly the call someone goes looking for.
    tlog = cglib.CapturingLog("voice")
    calls = {"n": 0}
    def spy(kind, query, status=None): calls["n"] += 1
    _saved_span, assistant.tracing.tool_span = assistant.tracing.tool_span, spy
    schemas = assistant.function_schemas(
        {"get_now_playing": lambda a: {"ok": True, "game": "Hades"},
         "launch_game": boom}, tlog)
    class P2:
        arguments = {"appid": 1145360}
        async def result_callback(self, out): pass
    for s in schemas:
        _a.run(s.handler(P2()))
    assistant.tracing.tool_span = _saved_span
    # Order follows TOOL_DEFS, not the impls dict, so key by tool name.
    rec = {r["tool"]: r for r in tlog.records if r.get("event") == "tool_call"}
    assert set(rec) == {"get_now_playing", "launch_game"}, rec   # the raiser too
    assert rec["get_now_playing"]["ok"] is True
    assert rec["launch_game"]["ok"] is False, "a raising impl must still record"
    assert "1145360" in rec["launch_game"]["args"]   # args carried, for search terms
    assert calls["n"] == 2, "tool_span not called per tool"
    # log=None (REPL/bench/tests) stays quiet rather than crashing.
    assistant.function_schemas({"get_now_playing": boom})[0]

    # --- nav tool: target->kind remap + catalog guard (only dispatch.nav was
    # covered before, in test_dispatch) --------------------------------------
    seen = []
    d.nav = lambda kind, arg=None: (seen.append((kind, arg)),
                                    types.SimpleNamespace(ok=True, detail="showing"))[1]
    navimpls = assistant.tool_impls(d, log)
    assert navimpls["nav"]({"target": "game_page", "appid": real_appid})["ok"]
    assert navimpls["nav"]({"target": "store_page", "appid": real_appid})["ok"]
    assert navimpls["nav"]({"target": "downloads"})["ok"]
    assert seen == [("details", real_appid), ("store", real_appid), ("downloads", None)], seen
    assert not navimpls["nav"]({"target": "bogus"})["ok"]
    # An UNOWNED appid: the LIBRARY page is refused (they have no such page),
    # the STORE page is NOT - that is exactly who a store page is for. The
    # couch refused "open the store page for Big Walk" over this, and with the
    # install dialog needing a button press either way, this IS the install
    # path (2026-08-14).
    assert not navimpls["nav"]({"target": "game_page", "appid": 999999999})["ok"]
    assert navimpls["nav"]({"target": "store_page", "appid": 1478500})["ok"]
    assert seen[-1] == ("store", 1478500), seen[-1]
    assert not navimpls["nav"]({"target": "store_page", "appid": 0})["ok"]

    # --- list_games success routing + get_game_details hltb-fallback (offline
    # via mocked fetchers; test_deals covers the fetchers themselves) ---------
    saved = (library.load_deals, library.fetch_trending, library.fetch_recently_played,
             library._store_items, library.fetch_hltb)
    library.load_deals = lambda: {"specials": [{"appid": 1, "name": "S"}],
                                  "wishlist_on_sale": [{"appid": 2, "name": "W"}]}
    library.fetch_trending = lambda: [{"appid": 3, "name": "T", "rank": 1}]
    library.fetch_recently_played = lambda: [{"appid": 4, "name": "R", "hours2w": 2.0}]
    fresh = assistant.tool_impls(d, log)
    assert fresh["list_games"]({"source": "specials"})["games"][0]["name"] == "S"
    assert fresh["list_games"]({"source": "wishlist_on_sale"})["games"][0]["name"] == "W"
    assert fresh["list_games"]({"source": "trending"})["games"][0]["name"] == "T"
    assert fresh["list_games"]({"source": "recently_played"})["games"][0]["name"] == "R"
    # hltb for a game with no catalog name resolves the name from the store,
    # instead of the old "unknown appid" dead end.
    hltb_calls = []
    library._store_items = lambda a, cc=None: {a[0]: {"name": "Some Unowned Game"}}
    library.fetch_hltb = lambda name: hltb_calls.append(name) or {"main": 20}
    r = fresh["get_game_details"]({"appid": 424242, "facets": ["hltb"]})
    assert r["ok"] and r.get("hltb") == {"main": 20} and hltb_calls == ["Some Unowned Game"], r
    (library.load_deals, library.fetch_trending, library.fetch_recently_played,
     library._store_items, library.fetch_hltb) = saved
    print("  fail-soft backstop, nav remap+guard, list_games routing, hltb name-fallback")

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
    # Closing the mic must never read as ending the session on the TV - the
    # contrast is spelled out in BOTH places the model reads it.
    assert "NOT end_session" in str(assistant.TOOL_DEFS)
    assert "never end the gaming session for them" in si
    assert set(assistant.BACKENDS) == {"anthropic", "openai"}
    assert assistant.BACKENDS["anthropic"].key == "anthropicApiKey"
    assert assistant.BACKENDS["openai"].key == "openaiApiKey"
    print(f"  tool renderers: {len(at)} anthropic + {len(ot)} openai, both cover all")

    # Server-side search: knob off -> absent everywhere, prompt included; knob
    # on -> each provider's NATIVE entry NEXT TO, never instead of, the tools.
    voice_off = CFG_MIN["voice"]
    assert assistant.server_tools(voice_off, "anthropic") == []
    assert assistant.server_tools(voice_off, "openai") == []
    assert "search the web" not in si
    voice_on = {**voice_off, "assistantWebSearch": True,
                "location": {"city": "Portland", "region": "",
                             "country": "US", "timezone": ""}}
    aw, = assistant.server_tools(voice_on, "anthropic")
    ow, = assistant.server_tools(voice_on, "openai")
    assert aw["type"] == "web_search_20250305" and aw["max_uses"] == 2
    assert ow["type"] == "web_search" and ow["search_context_size"] == "low"
    assert aw["user_location"] == ow["user_location"] == {
        "type": "approximate", "city": "Portland", "country": "US"}
    bare = {**voice_on, "location": {"city": "", "region": "", "country": "",
                                     "timezone": ""}}
    assert "user_location" not in assistant.server_tools(bare, "openai")[0]
    si_on = assistant.system_instruction({**CFG_MIN, "voice": voice_on})
    # Two spoken-register guardrails: no citations in TTS, no narrating search.
    assert "search the web" in si_on and "NO citations" in si_on
    assert "Never announce or offer to search" in si_on
    print("  server_tools: knob-gated, provider-native shapes, location folding")

    # pause_turn (a long server-side search pauses the turn): the partial
    # assistant content is re-sent as-is and the text accumulates - the
    # API's documented contract.
    import types
    b = assistant.AnthropicBackend({"anthropicApiKey": "x" * 24},
                                   "claude-haiku-4-5", voice=voice_on)
    script = [
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="server_tool_use"),
                     types.SimpleNamespace(type="text", text="Checking.")],
            stop_reason="pause_turn", usage=None),
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="June 2026.")],
            stop_reason="end_turn", usage=None),
    ]
    calls = []
    b.client = types.SimpleNamespace(messages=types.SimpleNamespace(
        create=lambda **kw: (calls.append(kw), script.pop(0))[1]))
    out = b.turn("sys", "when did the dlc ship", {})
    assert out == "Checking. June 2026.", out
    assert len(calls) == 2 and not script
    assert calls[0]["tools"][-1]["type"] == "web_search_20250305"
    assert calls[1]["messages"][-1]["role"] == "assistant"   # partial re-sent
    print("  pause_turn: continuation re-sent as-is, text accumulated")

    # Pipecat constructions with dummy keys (no network at init), both
    # providers, built through the PRODUCTION _make_llm: a test-local copy can
    # pass a dict for `reasoning`, which the settings accept silently and only
    # live inference rejects.
    schemas = assistant.function_schemas(impls)
    assert len(schemas) == 11       # the full surface; install_game is always on
    import session_runtime
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair)
    from pipecat.services.deepgram.tts import DeepgramTTSService
    ctx = LLMContext(messages=[], tools=schemas)
    ua, aa = LLMContextAggregatorPair(ctx)
    dummy = {"anthropicApiKey": "x" * 24, "openaiApiKey": "x" * 24}
    voice_a = {**CFG_MIN["voice"], "assistantProvider": "anthropic",
               "assistantModelAnthropic": "claude-haiku-4-5",
               "assistantModelOpenai": "gpt-5.6-luna"}
    llm_a = session_runtime._make_llm(voice_a, dummy, si)
    voice_o = {**voice_a, "assistantProvider": "openai",
               "assistantModelOpenai": "gpt-5.6-luna",
               "assistantReasoningEffort": "low"}
    llm_o = session_runtime._make_llm(voice_o, dummy, si)
    # The inference path's model_dump() call, which a plain dict would fail.
    assert llm_o._settings.reasoning.model_dump(exclude_none=True) == {
        "effort": "low"}, llm_o._settings.reasoning
    tts = DeepgramTTSService(api_key="x" * 24, sample_rate=16000,
                             settings=DeepgramTTSService.Settings(
                                 voice="aura-2-thalia-en"))
    assert ua and aa and llm_a and llm_o and tts
    # Native tools ride ToolsSchema.custom_tools through the OpenAI Responses
    # adapter VERBATIM, after the function tools - the shape run_session builds.
    from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
    ts = ToolsSchema(standard_tools=schemas,
                     custom_tools={AdapterType.OPENAI:
                                   assistant.server_tools(voice_on, "openai")})
    rendered = llm_o.get_llm_adapter().to_provider_tools_format(ts)
    assert [t["name"] for t in rendered[:-1]] == [s.name for s in schemas]
    assert rendered[-1]["type"] == "web_search"
    assert LLMContext(messages=[], tools=ts)
    print("  ToolsSchema: native web_search rendered after the function tools")
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
