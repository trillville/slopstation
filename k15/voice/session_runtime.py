"""One voice session: the per-wake Pipecat pipeline (mic -> Flux STT ->
GrammarGate -> speaker, plus the optional LLM assistant lane) and the
cross-session context carry. "Session" here is the voice session, not the
couch session (the lock in state/).

Heavy imports (pipecat, provider SDKs) stay INSIDE Session.run so importing
this module is cheap.
"""
import time

import cglib
import library
import titles
import traces
import tracing

log = cglib.make_log("voice")


# Deepgram's undocumented ceiling, measured 2026-08-15: 100 keyterms connect,
# 110 get HTTP 400 at the websocket handshake. A COUNT limit, not URL length
# (100 long terms, 4.7 kB, pass). Over it, every session fails to open.
MAX_KEYTERMS = 100


def load_titles(count, rows=None):
    """Installed titles by recency, spelled as Steam writes them."""
    if rows is None:
        rows = library.load().get("installed", [])
    rows = sorted(rows, key=lambda r: r.get("lastPlayed", 0), reverse=True)
    return [r["name"] for r in rows
            if r.get("name") and r.get("appid") not in library.NOT_GAMES][:count]


def stt_keyterms(voice, wake_phrase, catalog=None):
    """Everything Flux is told to expect, in the form it will hear it:
    titles, collection names, tag/genre words.

    Order is the budget policy - the cap truncates the tail, so generic tag
    words ('action', 'adventure') come last; Flux already knows those, an
    unseen title it does not. 2026-08: 92 of 100 slots spent (55 title forms
    from 39 games, 11 collections, 30 tag words). The 162 owned-but-uninstalled
    titles do not fit, which is why keyterm_forms covers 'hades' off Hades II
    rather than the list carrying plain Hades. Truncation is logged out loud on
    stt_vocabulary - a silently short list reads as full coverage."""
    catalog = catalog or library.Catalog.load()
    terms = [wake_phrase]
    for name in load_titles(voice["keytermCount"], catalog.installed):
        terms += titles.keyterm_forms(name)
    terms += [titles.spoken_form(c["name"])
              for c in catalog.collections if c.get("name")]
    terms += library.query_terms()

    seen, out = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    if len(out) > MAX_KEYTERMS:
        log.warn("keyterms_capped", kept=MAX_KEYTERMS, dropped=len(out) - MAX_KEYTERMS,
                 first_dropped=out[MAX_KEYTERMS])
        out = out[:MAX_KEYTERMS]
    return out


CARRY = {"messages": [], "t": 0.0}      # cross-session context (followupCarryS)


def job_messages(jobs, before_t):
    """Recent background results as prior conversation (both the ask and the
    result). Returns (pre, post), split on whether the job finished before
    before_t, the carry snapshot; the caller places each side around the
    carried turns to keep clock order.

    Open risk: worker text derives from untrusted web pages and is seeded as
    role:"assistant" in a context holding quit_game, install_game and nav,
    bounded only by those tools' checks (quit confirms first, install validates
    ownership, nav takes validated kinds only) and by bench/probe_intent.py.
    Hardening the worker's tool surface does NOT close this - the channel is
    its OUTPUT. The untried fix: seed results as a user-role quote attributed
    to the worker, or mark them untrusted in the text, then measure that the
    model treats them as data."""
    if jobs is None:
        return [], []
    import jobs as jobs_mod
    pre, post = [], []
    for j in jobs.for_context():
        said = j["summary"]
        detail = j.get("detail") or ""
        if len(detail) > jobs_mod.CONTEXT_DETAIL_CHARS:
            # Cut on a word and mark it: a silent mid-word cut reads to the
            # model as the whole report (2026-08-15).
            cut = detail[:jobs_mod.CONTEXT_DETAIL_CHARS]
            sp = cut.rfind(" ")
            detail = (cut[:sp] if sp > 0 else cut) + " [truncated]"
        if detail and detail != j["summary"]:
            said += " " + detail
        asked = (j.get("asked") or "").strip()
        msgs = pre if j.get("finished", 0) <= before_t else post
        if asked:
            msgs += [{"role": "user", "content": asked},
                     {"role": "assistant", "content": said}]
        else:
            # No transcript to quote (chord lane, REPL, or a job predating
            # `asked`). System role, not an invented user turn - text in the
            # user's mouth would read as a standing instruction.
            msgs += [{"role": "system",
                      "content": f"(earlier background task: {j['task']}) "
                                 f"You reported: {said}"}]
    return pre, post


def _trim_carry(messages):
    """Carry only whole tool exchanges: the Anthropic API 400s on a
    tool_result without its tool_use. Drop from the front until a plain user
    turn, and drop a trailing assistant-with-tool_calls that has no result."""
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
    """The LLM, chosen by config.assistantProvider. OpenAI goes through the
    Responses API, the one that lets reasoning and tools coexist."""
    from assistant import default_model
    provider = voice["assistantProvider"]
    if provider == "openai":
        from pipecat.services.openai.responses.llm import (
            OpenAIResponsesHttpLLMService, OpenAIResponsesReasoningConfig)
        # reasoning must be the typed config, not a dict: pipecat accepts
        # anything at construction, then calls .model_dump() at inference.
        return OpenAIResponsesHttpLLMService(
            api_key=secrets["openaiApiKey"],
            settings=OpenAIResponsesHttpLLMService.Settings(
                model=default_model(voice, "openai"),
                system_instruction=system_text, max_completion_tokens=1500,
                reasoning=OpenAIResponsesReasoningConfig(
                    effort=voice["assistantReasoningEffort"])))
    from pipecat.services.anthropic.llm import AnthropicLLMService
    return AnthropicLLMService(
        api_key=secrets["anthropicApiKey"],
        settings=AnthropicLLMService.Settings(
            model=default_model(voice, "anthropic"),
            system_instruction=system_text,
            enable_prompt_caching=True, max_tokens=400))


class Session:
    """One voice session, from a wake to idle or an exit phrase: build the
    pipeline (mic -> Flux -> GrammarGate -> speaker, plus the LLM lane), run
    it, then save the transcript and the carry."""

    def __init__(self, cfg, secrets, matcher, dry_run, input_idx, output_idx,
                 capture=None, jobs=None, ack=None, steam=None,
                 on_end_session=None):
        self.cfg, self.secrets, self.matcher = cfg, secrets, matcher
        self.dry_run = dry_run
        self.input_idx, self.output_idx = input_idx, output_idx
        self.capture, self.jobs, self.ack, self.steam = capture, jobs, ack, steam
        self.on_end_session = on_end_session    # the room ducker's restore
        self.voice = cfg["voice"]
        self.provider = self.voice["assistantProvider"]
        self.context = None                     # the LLM lane's, once built
        self.seeded = (0, 0, 0)                 # (jobs-before, carried, jobs-after)

    async def run(self):
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

        from assistant import PROVIDER_KEY
        from audio import wake_phrase as _wake_phrase
        from dispatch import Dispatch
        from grammar_gate import GrammarGate
        from preroll import PrerollFeeder

        cfg, secrets, voice = self.cfg, self.secrets, self.voice
        catalog = library.Catalog.load()
        game_terms = load_titles(voice["keytermCount"], catalog.installed)
        # Keyterm-boosted so the pre-roll wake phrase transcribes canonically
        # and strip_wake matches.
        wake_phrase = _wake_phrase(voice["wakeModel"])
        keyterms = stt_keyterms(voice, wake_phrase, catalog)
        log("stt_vocabulary", terms=len(keyterms), titles=len(game_terms),
            headroom=MAX_KEYTERMS - len(keyterms))

        transport = LocalAudioTransport(LocalAudioTransportParams(
            audio_in_enabled=True, audio_in_sample_rate=16000,
            audio_out_enabled=True, audio_out_sample_rate=16000,
            input_device_index=self.input_idx,
            output_device_index=self.output_idx,
        ))

        stt = DeepgramFluxSTTService(
            api_key=secrets["deepgramApiKey"],
            sample_rate=16000,
            mip_opt_out=True,                   # privacy over the metered rate
            settings=DeepgramFluxSTTService.Settings(
                model="flux-general-en",
                eot_threshold=voice["eotThreshold"],
                # Dormant: pipecat 1.7 forwards Flux's EagerEndOfTurn as an
                # InterimTranscriptionFrame, which GrammarGate (finals only)
                # never sees. Its one live effect is resetting the idle clock.
                eager_eot_threshold=(voice["eagerEotThreshold"]
                                     if voice.get("eagerEnabled", True) else None),
                numerals=True,
                keyterm=keyterms,
            ),
        )

        dispatcher = Dispatch(cfg, log, dry_run=self.dry_run,
                              on_end_session=self.on_end_session)
        assistant_live = cglib.real_key(secrets.get(PROVIDER_KEY[self.provider]))
        gate = GrammarGate(
            self.matcher, dispatcher, log,
            resolve_game=(titles.build_resolver(voice["fuzzyTitleThreshold"],
                                                rows=catalog.installed)
                          if game_terms else None),
            resolve_collection=titles.build_collection_resolver(
                voice["fuzzyTitleThreshold"],
                rows=catalog.collections),      # None when no collections synced
            assistant_enabled=assistant_live,
            wake_word=wake_phrase.split()[-1],  # "jarvis" - the strip anchor
            jobs=self.jobs,
            ack=self.ack,                       # wake chime, if still unplayed
        )

        feeder = PrerollFeeder(log)
        stages = [transport.input(), feeder, stt, gate]
        if assistant_live:
            stages += self._assistant_stages(transport, dispatcher, gate)
        else:
            stages += [transport.output()]

        # No barge-in: nothing here constructs an InterruptionWorkerFrame and
        # the transport has no vad_analyzer, so a Tier-1 command spoken
        # mid-answer DISPATCHES while the answer keeps playing over it.
        #
        # enable_metrics is what populates token counts and TTFB in the spans;
        # enable_tracing belongs on PipelineWorker, not PipelineTask.
        tracing_on = tracing.is_on()
        worker = PipelineWorker(
            Pipeline(stages),
            params=PipelineParams(audio_in_sample_rate=16000,
                                  audio_out_sample_rate=16000,
                                  enable_metrics=tracing_on),
            enable_rtvi=False,
            enable_tracing=tracing_on,
            enable_turn_tracking=tracing_on,
            # Pipecat's conversation id IS our session id: a Langfuse trace
            # and the JSONL lines around it join on this value.
            conversation_id=tracing.conversation_id() if tracing_on else None,
            additional_span_attributes=tracing.span_attributes() if tracing_on else None,
            idle_timeout_secs=voice["holdWindowS"],
            idle_timeout_frames=(TranscriptionFrame, InterimTranscriptionFrame,
                                 UserStartedSpeakingFrame, BotSpeakingFrame),
            cancel_on_idle_timeout=False,       # we decide - see the handler
        )

        @worker.event_handler("on_idle_timeout")
        async def _on_idle(worker):
            # Flux emits no frame mid-turn and dispatch pushes nothing while
            # it blocks, so the idle clock can expire mid-utterance or mid-ssh.
            if gate.is_busy():
                log("idle_deferred", reason="busy")
                return
            log("session_idle_timeout")
            await worker.cancel(reason="idle")

        runner = WorkerRunner(handle_sigint=False)
        if self.capture is not None:
            # Stop as late as possible: only worker start + mic-open remain,
            # so the uncaptured gap is ~100-200 ms, not the whole session
            # build.
            feeder.pcm = self.capture.stop()
        try:
            await runner.add_workers(worker)
            if self.jobs is not None and assistant_live and self.jobs.unread():
                # Next-wake mention for an aborted or synth-failed announcement.
                await worker.queue_frame(TTSSpeakFrame(
                    "By the way, a background task finished - say what did "
                    "you find to hear it."))
            await runner.run()
        finally:
            # pipecat 1.7 never terminates the PyAudio handle it creates and
            # exposes no public cleanup; a fresh transport per wake would leak
            # one each time. Guarded so an upstream rename logs, not crashes.
            pa = getattr(transport, "_pyaudio", None)
            if pa is not None:
                try:
                    pa.terminate()
                except Exception as e:
                    log.warn("pyaudio_terminate_failed", err=str(e))
        self._save_and_carry()

    def _assistant_stages(self, transport, dispatcher, gate):
        """The LLM lane: a context seeded from the carry and recent background
        results, the tools, the provider's LLM, TTS. The stages after the
        gate."""
        from assistant import (function_schemas, server_tools,
                               system_instruction, tool_impls)
        from pipecat.adapters.schemas.tools_schema import (AdapterType,
                                                           ToolsSchema)
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair)

        voice, secrets, jobs = self.voice, self.secrets, self.jobs
        carry = (list(CARRY["messages"])
                 if time.time() - CARRY["t"] < voice["followupCarryS"] else [])
        # Background results go in history, not the system prompt, which must
        # stay byte-identical session to session or the prompt cache misses.
        # Clock order matters: a job finishing after the last session goes
        # AFTER the carried turns, or the model reads it as answered-then-
        # asked and denies having the result (2026-08-15).
        pre, post = job_messages(jobs, CARRY["t"])
        self.seeded = (len(pre), len(carry), len(post))
        carry = pre + carry + post
        # Native (provider-executed) tools ride custom_tools. Only the OpenAI
        # adapter has that passthrough in pipecat 1.7 (AdapterType has no
        # ANTHROPIC), so the knob is a no-op under the anthropic provider.
        native = server_tools(voice, "openai") if self.provider == "openai" else []
        self.context = LLMContext(
            messages=carry,
            tools=ToolsSchema(
                standard_tools=function_schemas(
                    # Built after the gate so its request_stop can ride here.
                    tool_impls(dispatcher, log, jobs=jobs,
                               on_stop_listening=gate.request_stop,
                               voice=voice, steam=self.steam),
                    log),                   # -> one tool_call event per call
                custom_tools={AdapterType.OPENAI: native} if native else None))
        user_agg, asst_agg = LLMContextAggregatorPair(self.context)
        llm = _make_llm(voice, secrets, system_instruction(self.cfg))
        if native:
            # Pipecat 1.7 has no handling for provider-executed tools: a
            # web_search never reaches the context, so the model cannot tell
            # that it searched.
            import llm_audit
            if llm_audit.install(llm, log, tracing=tracing, context=self.context):
                log("lane_up", what="search_audit", tools=len(native))
            else:
                log.warn("lane_disabled", what="search_audit",
                         reason="pipecat client shape moved - searches will "
                                "be invisible again")
        return [user_agg, llm, _make_tts(voice, secrets), transport.output(),
                asst_agg]

    def _save_and_carry(self):
        """Cross-session follow-ups: dump the transcript, carry the last turns
        minus the seeded job results - the next session re-seeds them from
        jobs.json, so carrying them too would double the findings until they
        aged out. They sit on both sides of the carried block; cut by the
        counts recorded at build time."""
        if self.context is None:
            return
        msgs = list(self.context.messages)
        traces.save("voice", msgs,
                    {"provider": self.provider, "dry_run": self.dry_run})
        n_pre, n_carry, n_post = self.seeded
        kept = msgs[n_pre:n_pre + n_carry] + msgs[n_pre + n_carry + n_post:]
        CARRY["messages"] = _trim_carry(kept[-8:])
        CARRY["t"] = time.time()


async def run_session(cfg, secrets, matcher, dry_run, input_idx, output_idx,
                      capture=None, jobs=None, ack=None, steam=None,
                      on_end_session=None):
    """voice_agent's entry: one Session, run to its end."""
    await Session(cfg, secrets, matcher, dry_run, input_idx, output_idx,
                  capture=capture, jobs=jobs, ack=ack, steam=steam,
                  on_end_session=on_end_session).run()
