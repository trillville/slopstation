"""Construction and lifecycle of ONE voice session: the per-wake Pipecat
pipeline (mic -> Flux STT -> GrammarGate -> speaker, with the optional LLM
assistant lane), plus the cross-session carry that lets a follow-up land
seconds after a session closed.

Named session_runtime, not session: on the K15 "session" already means the
couch session - the lock in state/ - and this is a different thing.

Heavy imports (pipecat, the provider SDKs) stay INSIDE run_session: a session
build pays them, importing this module does not.
"""
import time

import cglib
import library
import titles
import traces
import tracing

log = cglib.make_log("voice")


def load_titles(count):
    """Installed titles by recency: Flux keyterms + fuzzy-resolution corpus."""
    rows = library.load().get("installed", [])
    rows.sort(key=lambda r: r.get("lastPlayed", 0), reverse=True)
    return [r["name"] for r in rows if r.get("name")][:count]


CARRY = {"messages": [], "t": 0.0}      # cross-session context (followupCarryS)


def job_messages(jobs):
    """Recent background results as prior conversation - the worker's answer
    in the assistant's mouth, so a follow-up needs no re-explaining. Task and
    result both go in: "which one was cheapest?" needs the findings, "why did
    you look that up?" needs the ask."""
    if jobs is None:
        return []
    import jobs as jobs_mod
    msgs = []
    for j in jobs.for_context():
        said = j["summary"]
        detail = (j.get("detail") or "")[:jobs_mod.CONTEXT_DETAIL_CHARS]
        if detail and detail != j["summary"]:
            said += " " + detail
        asked = (j.get("asked") or "").strip()
        if asked:
            # A true exchange: what the user said, then what came back.
            msgs += [{"role": "user", "content": asked},
                     {"role": "assistant", "content": said}]
        else:
            # No transcript to quote (chord lane, REPL, or a job recorded
            # before `asked` existed). State it as history rather than
            # inventing a user turn - putting the model's own brief in the
            # user's mouth is what made "using only games in the provided
            # catalog" read as a standing instruction for six hours.
            msgs += [{"role": "system",
                      "content": f"(earlier background task: {j['task']}) "
                                 f"You reported: {said}"}]
    return msgs


def _trim_carry(messages):
    """Carry only whole tool exchanges. A fixed slice can start on an orphaned
    role:'tool' message (or an assistant message bearing tool_calls with no
    following result); the Anthropic API 400s on a tool_result without its
    tool_use, so drop from the front until the first message is a plain user
    turn, and drop a trailing assistant-with-tool-calls that has no result."""
    def is_plain_user(m):
        return (isinstance(m, dict) and m.get("role") == "user"
                and "tool_call_id" not in m and "tool_use_id" not in m)

    def has_tool_calls(m):
        return isinstance(m, dict) and (m.get("tool_calls")
                                        or m.get("role") == "assistant"
                                        and isinstance(m.get("content"), list)
                                        and any(isinstance(b, dict)
                                                and b.get("type") == "tool_use"
                                                for b in m["content"]))
    msgs = list(messages)
    while msgs and not is_plain_user(msgs[0]):
        msgs.pop(0)
    if msgs and has_tool_calls(msgs[-1]):
        msgs.pop()
    return msgs


def _make_tts(voice, secrets):
    if voice.get("ttsLocal"):
        try:
            from pipecat.services.kokoro.tts import KokoroTTSService
            log("tts_selected", engine="kokoro", local=True)
            return KokoroTTSService()
        except Exception as e:
            log.warn("tts_fallback", wanted="kokoro", using="aura-2", err=str(e))
    from pipecat.services.deepgram.tts import DeepgramTTSService
    return DeepgramTTSService(
        api_key=secrets["deepgramApiKey"], sample_rate=16000,
        settings=DeepgramTTSService.Settings(voice=voice["ttsVoice"]))


def _make_llm(voice, secrets, system_text):
    """The brain, provider-switchable via config.assistantProvider - so once the
    --text A/B picks a winner, production follows by flipping one config key.
    OpenAI uses the Responses API (reasoning + tools coexist there); effort is
    a config knob that trades latency for depth."""
    from assistant import default_model
    provider = voice["assistantProvider"]
    if provider == "openai":
        from pipecat.services.openai.responses.llm import (
            OpenAIResponsesHttpLLMService, OpenAIResponsesReasoningConfig)
        # reasoning must be the TYPED config, not a dict: pipecat's dataclass
        # settings accept anything at construction, then call .model_dump()
        # at inference - a dict here dies with "'dict' object has no
        # attribute 'model_dump'".
        return OpenAIResponsesHttpLLMService(
            api_key=secrets["openaiApiKey"],
            settings=OpenAIResponsesHttpLLMService.Settings(
                model=default_model({"voice": voice}, "openai"),
                system_instruction=system_text, max_completion_tokens=1500,
                reasoning=OpenAIResponsesReasoningConfig(
                    effort=voice["assistantReasoningEffort"])))
    from pipecat.services.anthropic.llm import AnthropicLLMService
    return AnthropicLLMService(
        api_key=secrets["anthropicApiKey"],
        settings=AnthropicLLMService.Settings(
            model=default_model({"voice": voice}, "anthropic"),
            system_instruction=system_text,
            enable_prompt_caching=True, max_tokens=400))


async def run_session(cfg, secrets, matcher, args, input_idx, output_idx,
                      capture=None, jobs=None, ack=None):
    from pipecat.frames.frames import (BotSpeakingFrame,
                                       InterimTranscriptionFrame,
                                       TranscriptionFrame, TTSSpeakFrame,
                                       UserStartedSpeakingFrame)
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
    from pipecat.transports.local.audio import (LocalAudioTransport,
                                                LocalAudioTransportParams)
    from pipecat.workers.runner import WorkerRunner

    from dispatch import Dispatch
    from grammar_gate import GrammarGate
    from preroll import PrerollFeeder

    voice = cfg["voice"]
    game_terms = load_titles(voice["keytermCount"])
    # "hey_jarvis_v0.1" -> "hey jarvis"; keyterm-boosted so the pre-roll's wake
    # phrase transcribes canonically and strip_wake lands every time.
    wake_phrase = voice["wakeModel"].rsplit("_v", 1)[0].replace("_", " ")

    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_in_sample_rate=16000,
        audio_out_enabled=True, audio_out_sample_rate=16000,
        input_device_index=input_idx, output_device_index=output_idx,
    ))

    stt = DeepgramFluxSTTService(
        api_key=secrets["deepgramApiKey"],
        sample_rate=16000,
        mip_opt_out=True,                       # privacy over the metered rate
        settings=DeepgramFluxSTTService.Settings(
            model="flux-general-en",
            eot_threshold=voice["eotThreshold"],
            eager_eot_threshold=(voice["eagerEotThreshold"]
                                 if voice.get("eagerEnabled", True) else None),
            numerals=True,
            # Titles teach Flux the game names; query_terms teach it the
            # words used to ask ABOUT them (tags/genres: "mech", "roguelike")
            # - without them "mech games" transcribed as "met games".
            keyterm=[wake_phrase] + game_terms + library.query_terms(),
        ),
    )

    dispatcher = Dispatch(cfg, log, dry_run=args.dry_run)
    from assistant import BACKENDS
    provider = voice["assistantProvider"]
    assistant_live = cglib.real_key(secrets.get(BACKENDS[provider].key))
    gate = GrammarGate(
        matcher, dispatcher, log,
        resolve_game=(titles.build_resolver(voice["fuzzyTitleThreshold"])
                      if game_terms else None),
        assistant_enabled=assistant_live,
        wake_word=wake_phrase.split()[-1],      # "jarvis" - the strip anchor
        jobs=jobs,
        ack=ack,                                # wake chime, if still unplayed
    )

    feeder = PrerollFeeder(log)
    stages = [transport.input(), feeder, stt, gate]
    context = None
    seeded = []                                 # job results at the front
    if assistant_live:
        from assistant import (function_schemas, server_tools,
                               system_instruction, tool_impls)
        from pipecat.adapters.schemas.tools_schema import (AdapterType,
                                                           ToolsSchema)
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair)

        carry = (list(CARRY["messages"])
                 if time.time() - CARRY["t"] < voice["followupCarryS"] else [])
        # Background results lead the history, so "which one was cheapest?"
        # after an announcement lands on an assistant that knows what it
        # found. History, not system prompt: the system block stays
        # byte-identical session to session, which is what the prompt cache
        # keys on (a volatile tail would cost the catalog's cache read).
        seeded = job_messages(jobs)
        carry = seeded + carry
        # Native (provider-executed) tools ride custom_tools - the adapter
        # appends them verbatim after the function tools. Only the OpenAI
        # adapter has this passthrough in pipecat 1.7 (AdapterType has no
        # ANTHROPIC), so with the anthropic provider the knob reaches the
        # REPL but not production - main() logs that at startup.
        native = server_tools(voice, "openai") if provider == "openai" else []
        context = LLMContext(
            messages=carry,
            tools=ToolsSchema(
                standard_tools=function_schemas(
                    tool_impls(dispatcher, log, jobs=jobs)),
                custom_tools={AdapterType.OPENAI: native} if native else None))
        user_agg, asst_agg = LLMContextAggregatorPair(context)
        llm = _make_llm(voice, secrets, system_instruction(cfg))
        if native:
            # Pipecat 1.7 has no handling for provider-executed tools, so a
            # web_search streams past unrecorded and never reaches the
            # context - the model then cannot tell it searched. Only wire
            # this where such tools are actually enabled.
            import llm_audit
            if llm_audit.install(llm, log, tracing=tracing, context=context):
                log("lane_up", what="search_audit", tools=len(native))
            else:
                log.warn("lane_disabled", what="search_audit",
                         reason="pipecat client shape moved - searches will "
                                "be invisible again")
        stages += [user_agg, llm,
                   _make_tts(voice, secrets), transport.output(), asst_agg]
    else:
        stages += [transport.output()]

    # Tracing is opt-in per session and costs nothing when off. enable_metrics
    # is what populates the token counts and TTFB that make the spans worth
    # having - without it the tree arrives with timings but no numbers.
    tracing_on = tracing.is_on()
    worker = PipelineWorker(
        Pipeline(stages),
        params=PipelineParams(audio_in_sample_rate=16000,
                              audio_out_sample_rate=16000,
                              enable_metrics=tracing_on),
        enable_rtvi=False,
        enable_tracing=tracing_on,
        enable_turn_tracking=tracing_on,
        # Pipecat's conversation id IS our session id, so a Langfuse trace and
        # the JSONL lines around it share one value to join on.
        conversation_id=tracing.conversation_id() if tracing_on else None,
        additional_span_attributes=tracing.span_attributes() if tracing_on else None,
        idle_timeout_secs=voice["holdWindowS"],
        idle_timeout_frames=(TranscriptionFrame, InterimTranscriptionFrame,
                             UserStartedSpeakingFrame, BotSpeakingFrame),
        cancel_on_idle_timeout=False,           # we decide - see the handler
    )

    @worker.event_handler("on_idle_timeout")
    async def _on_idle(worker):
        # Flux emits no frame mid-turn and dispatch pushes nothing while it
        # blocks, so the idle clock can expire mid-utterance or mid-ssh. End
        # only when the user isn't speaking and no dispatch is in flight;
        # otherwise defer and let the next idle window re-check.
        if gate.is_busy():
            log("idle_deferred", reason="busy")
            return
        log("session_idle_timeout")
        await worker.cancel(reason="idle")

    runner = WorkerRunner(handle_sigint=False)
    if capture is not None:
        # Stop as late as possible: every slow build step is behind us, only
        # worker start + the transport's mic-open remain, so the uncaptured
        # gap is ~100-200 ms instead of the whole session build.
        feeder.pcm = capture.stop()
    try:
        await runner.add_workers(worker)
        if jobs is not None and assistant_live and jobs.unread():
            # Next-wake mention (an aborted or synth-failed announcement
            # lands here): one line through the session TTS, result kept
            # unread until actually retrieved.
            await worker.queue_frame(TTSSpeakFrame(
                "By the way, a background task finished - say what did "
                "you find to hear it."))
        await runner.run()
    finally:
        # pipecat 1.7 owns but never terminates the PyAudio handle it creates,
        # and exposes no public cleanup - and we build a fresh transport per
        # wake, so this must run. Guard the private name: a future upstream
        # rename should leak one host handle with a log line, not crash.
        pa = getattr(transport, "_pyaudio", None)
        if pa is not None:
            try:
                pa.terminate()
            except Exception as e:
                log.warn("pyaudio_terminate_failed", err=str(e))
    if context is not None:                     # cross-session follow-ups
        msgs = list(context.messages)
        # Trace = the session's full context (carried turns included), i.e.
        # exactly what the model saw.
        traces.save("voice", msgs,
                    {"provider": provider, "dry_run": args.dry_run})
        # Drop the seeded job results before carrying: the next session seeds
        # them again from jobs.json, and carrying them too would double the
        # findings in context on every session until they aged out.
        CARRY["messages"] = _trim_carry(msgs[len(seeded):][-8:])
        CARRY["t"] = time.time()
