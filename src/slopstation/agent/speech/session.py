"""Build and run one Pipecat voice session."""

from slopstation import config, logbook
from slopstation.agent.speech import carry, keyterms, tool_schemas
from slopstation.agent.telemetry import sentry, traces
from slopstation.agent.tools import library, titles

log = logbook.logger("voice")


def busy_stage(voice, log, toolkit, gate=None):
    """The busy tone stage, or None when off. A tool call still running after
    busyAfterMs gets one phrase per turn: the tool's own, else busyPhrase, else
    the earcon."""
    if not voice.get("busyEnabled", True):
        return None
    after_ms = int(voice.get("busyAfterMs", 800) or 0)
    if after_ms <= 0:
        return None
    from slopstation.agent.speech.busy import BusyTone

    return BusyTone(
        log,
        after_s=after_ms / 1000,
        phrase=str(voice.get("busyPhrase", "") or ""),
        phrases=toolkit.busy_phrase if toolkit is not None else None,
        on_phrase=gate.expect_filler if gate is not None else None,
    )


def _make_tts(voice, secrets):
    from pipecat.services.deepgram.tts import DeepgramTTSService

    from slopstation.agent.speech.spoken import SpokenText

    return DeepgramTTSService(
        api_key=secrets["deepgramApiKey"],
        sample_rate=16000,
        settings=DeepgramTTSService.Settings(voice=voice["ttsVoice"]),
        text_filters=[SpokenText()],
    )


def _make_llm(voice, secrets, system_text):
    """Create the configured assistant provider."""
    from slopstation.agent.llm.assistant import default_model

    provider = voice["assistantProvider"]
    if provider == "openai":
        from pipecat.services.openai.responses.llm import (
            OpenAIResponsesHttpLLMService,
            OpenAIResponsesReasoningConfig,
        )

        # A typed config, not a dict: pipecat calls .model_dump() on it at
        # inference.
        return OpenAIResponsesHttpLLMService(
            api_key=secrets["openaiApiKey"],
            settings=OpenAIResponsesHttpLLMService.Settings(
                model=default_model(voice, "openai"),
                system_instruction=system_text,
                max_completion_tokens=1500,
                reasoning=OpenAIResponsesReasoningConfig(
                    effort=voice["assistantReasoningEffort"]
                ),
            ),
        )
    from pipecat.services.anthropic.llm import AnthropicLLMService

    return AnthropicLLMService(
        api_key=secrets["anthropicApiKey"],
        settings=AnthropicLLMService.Settings(
            model=default_model(voice, "anthropic"),
            system_instruction=system_text,
            enable_prompt_caching=True,
            max_tokens=400,
        ),
    )


class _PipecatErrors:
    """Counts pipecat's logged errors. A failed speaker write pushes no frame,
    so without this a dead speaker looks like a clean session."""

    OUTPUT = "pipecat.transports.base_output"

    def __init__(self):
        self.count = 0
        self.output = 0
        self.first = ""
        self._sink: int | None = None

    def __enter__(self):
        try:
            from loguru import logger as loguru_log

            self._sink = loguru_log.add(
                self._record,
                level="ERROR",
                filter=lambda r: (r["name"] or "").startswith("pipecat."),
            )
        except Exception as e:
            log.warn("pipeline_watch_failed", err=str(e))
        return self

    def _record(self, message):
        record = message.record
        self.count += 1
        if record["name"] == self.OUTPUT:
            self.output += 1
        if not self.first:
            self.first = str(record["message"])[:200]

    def __exit__(self, *exc):
        if self._sink is not None:
            try:
                from loguru import logger as loguru_log

                loguru_log.remove(self._sink)
            except Exception:
                pass
        if self.count:
            log.error(
                "pipeline_error", err=self.first, count=self.count, output=self.output
            )
        return False


class Session:
    """A voice pipeline running from wake until idle or an exit phrase."""

    def __init__(
        self,
        services,
        matcher,
        input_idx,
        output_idx,
        capture=None,
        ack=None,
        on_end_session=None,
        room=None,
    ):
        self.services, self.matcher = services, matcher
        self.cfg, self.secrets = services.cfg, services.secrets
        self.dry_run = services.dry_run
        self.input_idx, self.output_idx = input_idx, output_idx
        self.capture = capture
        self.ack = ack
        self.on_end_session = on_end_session  # the room ducker's restore
        self.room = room  # voice.RoomState, or None when ducking is off
        self.voice = self.cfg["voice"]
        self.provider = self.voice["assistantProvider"]
        self.context = None  # the LLM lane's, once built
        self.toolkit = None  # the LLM lane's tools, once built
        self.audio_failed = False  # the speaker stopped taking frames

    async def run(self):
        from pipecat.workers.runner import WorkerRunner

        transport, feeder, gate, stages = self._build_stages()
        worker = self._build_worker(stages)

        @worker.event_handler("on_idle_timeout")
        async def _on_idle(worker):
            # Nothing emits frames mid-turn or while a command runs, so a busy
            # session is not idle.
            if gate.is_busy():
                log("idle_deferred", reason="busy")
                return
            log("session_idle_timeout")
            await worker.cancel(reason="idle")

        # Pipecat swallows setup errors; without this a failed build looks like
        # a clean close.
        started = False

        @worker.event_handler("on_pipeline_started")
        async def _on_started(worker, frame):
            nonlocal started
            started = True

        runner = WorkerRunner(handle_sigint=False)
        # Handed over live: the feeder stops it at StartFrame, so speech during
        # the Flux connect is kept. The chime deadline counts from the wake, so
        # it is disarmed here.
        if self.capture is not None:
            self.capture.disarm_deadline()
        feeder.capture = self.capture
        errors = _PipecatErrors()
        try:
            with errors:
                await runner.add_workers(worker)
                await runner.run()
                if not started:
                    raise RuntimeError(
                        "pipeline setup failed before StartFrame - "
                        "the underlying error is console-only"
                    )
        finally:
            # Pipecat 1.8.1 never frees its PyAudio handle; without this one
            # leaks per wake.
            pa = getattr(transport, "_pyaudio", None)
            if pa is not None:
                try:
                    pa.terminate()
                except Exception as e:
                    log.warn("pyaudio_terminate_failed", err=str(e))
        self.audio_failed = errors.output > 0
        self._save_and_carry()

    def _build_stages(self):
        """Mic through the grammar gate, then the LLM lane or the speaker.
        Returns (transport, feeder, gate, stages)."""
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        from pipecat.transports.local.audio import (
            LocalAudioTransport,
            LocalAudioTransportParams,
        )
        from pipecat.turns.user_turn_processor import UserTurnProcessor
        from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

        from slopstation.agent.llm.assistant import PROVIDER_KEY
        from slopstation.agent.speech.audio import wake_phrase as _wake_phrase
        from slopstation.agent.speech.grammar_gate import GrammarGate
        from slopstation.agent.speech.level import RoomLevel
        from slopstation.agent.speech.preroll import PrerollFeeder

        secrets, voice = self.secrets, self.voice
        catalog = library.Catalog.load()
        game_terms = keyterms.load_titles(voice["keytermCount"], catalog.installed)
        wake_phrase = _wake_phrase(voice["wakeModel"])
        terms = keyterms.stt_keyterms(voice, wake_phrase, catalog)
        log(
            "stt_vocabulary",
            terms=len(terms),
            titles=len(game_terms),
            headroom=keyterms.MAX_KEYTERMS - len(terms),
        )

        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=16000,
                audio_out_enabled=True,
                audio_out_sample_rate=16000,
                input_device_index=self.input_idx,
                output_device_index=self.output_idx,
            )
        )

        stt = DeepgramFluxSTTService(
            api_key=secrets["deepgramApiKey"],
            sample_rate=16000,
            mip_opt_out=True,  # privacy over the metered rate
            settings=DeepgramFluxSTTService.Settings(
                model="flux-general-en",
                eot_threshold=voice["eotThreshold"],
                # Pipecat currently exposes this only as an interim frame.
                eager_eot_threshold=(
                    voice["eagerEotThreshold"]
                    if voice.get("eagerEnabled", True)
                    else None
                ),
                numerals=True,
                keyterm=terms,
                # Flux ends a turn this long after speech stops; default 5 s.
                eot_timeout_ms=(
                    int(voice["eotTimeoutMs"]) if voice.get("eotTimeoutMs") else None
                ),
            ),
        )

        # Floor 0 measures but never mutes.
        loud = (lambda: self.room.loud) if self.room is not None else None
        level = RoomLevel(
            floor_db=float(voice.get("chatterFloorDb", 0) or 0), log=log, loud=loud
        )
        dispatcher = self.services.dispatch(on_end_session=self.on_end_session)
        assistant_live = config.real_key(secrets.get(PROVIDER_KEY[self.provider]))
        gate = GrammarGate(
            self.matcher,
            dispatcher,
            log,
            resolve_game=(
                titles.build_resolver(
                    voice["fuzzyTitleThreshold"], rows=catalog.installed
                )
                if game_terms
                else None
            ),
            resolve_collection=titles.build_collection_resolver(
                voice["fuzzyTitleThreshold"], rows=catalog.collections
            ),  # None when no collections synced
            assistant_enabled=assistant_live,
            wake_word=wake_phrase.split()[-1],  # "jarvis" - the strip anchor
            ack=self.ack,  # wake chime, if still unplayed
            # Read per turn: the duck lands off-thread.
            loud=loud,
            level=level,
            addressed=self.capture is None,  # a follow-up open: no wake word
        )

        feeder = PrerollFeeder(log)
        # Talking over the answer cuts it off.
        turns = UserTurnProcessor(
            user_turn_strategies=ExternalUserTurnStrategies(enable_interruptions=True)
        )
        stages = [transport.input(), feeder, level, stt, turns, gate]
        if assistant_live:
            stages += self._assistant_stages(transport, dispatcher, gate)
        else:
            stages += [transport.output()]
        return transport, feeder, gate, stages

    def _build_worker(self, stages):
        """The Pipecat worker over `stages`: tracing, and the idle clock."""
        from pipecat.frames.frames import (
            BotSpeakingFrame,
            InterimTranscriptionFrame,
            ProposedUserStartedSpeakingFrame,
            TranscriptionFrame,
            UserStartedSpeakingFrame,
        )
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker

        # Metrics fill token counts and time to first byte in the spans.
        tracing_on = sentry.is_on()
        worker = PipelineWorker(
            Pipeline(stages),
            params=PipelineParams(
                audio_in_sample_rate=16000,
                audio_out_sample_rate=16000,
                enable_metrics=tracing_on,
            ),
            enable_rtvi=False,
            enable_tracing=tracing_on,
            enable_turn_tracking=tracing_on,
            # Our session id, so a Sentry conversation joins the event log.
            conversation_id=sentry.conversation_id() if tracing_on else None,
            additional_span_attributes=sentry.span_attributes() if tracing_on else None,
            idle_timeout_secs=self.voice["holdWindowS"],
            # Any of these resets the idle clock.
            idle_timeout_frames=(
                TranscriptionFrame,
                InterimTranscriptionFrame,
                UserStartedSpeakingFrame,
                ProposedUserStartedSpeakingFrame,
                BotSpeakingFrame,
            ),
            cancel_on_idle_timeout=False,  # the handler decides
        )
        return worker

    def _assistant_stages(self, transport, dispatcher, gate):
        """The LLM lane: carried turns, tools, provider LLM, and TTS."""
        from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

        from slopstation.agent.llm.assistant import (
            server_tools,
            system_instruction,
        )

        voice, secrets = self.voice, self.secrets
        messages, loaded, carried_gate = carry.load(voice["followupCarryS"])
        # Provider-run tools (web search) work only with OpenAI in pipecat
        # 1.8.1.
        native = server_tools(voice, "openai") if self.provider == "openai" else []

        def tools_schema():
            assert self.toolkit is not None
            return ToolsSchema(
                standard_tools=tool_schemas.pipecat_schemas(self.toolkit),
                custom_tools={AdapterType.OPENAI: native} if native else None,
            )

        # Built after the gate, for request_stop. on_load runs on the tool's
        # thread; replacing the tool list is one assignment, so it is safe.
        self.toolkit = self.services.toolkit(
            dispatcher,
            on_stop_listening=gate.request_stop,
            on_load=lambda: (
                self.context.set_tools(tools_schema())
                if self.context is not None
                else None
            ),
            # A follow-up keeps the last session's open questions, so a late
            # yes still confirms.
            gate=carried_gate,
        )
        if loaded:
            self.toolkit.load(loaded)
        self.context = LLMContext(messages=messages, tools=tools_schema())
        # Passed so the aggregator skips its own turn model; turns are resolved
        # upstream.
        user_agg, asst_agg = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=ExternalUserTurnStrategies(
                    enable_interruptions=True
                )
            ),
        )
        llm = _make_llm(
            voice, secrets, system_instruction(self.cfg, offered=self.toolkit.offered)
        )
        if native:
            # Pipecat 1.8.1 does not record provider-run searches; without this
            # they are invisible.
            from slopstation.agent.llm import llm_audit

            if llm_audit.install(llm, log, spans=sentry, context=self.context):
                log("lane_up", what="search_audit", tools=len(native))
            else:
                log.warn(
                    "lane_disabled",
                    what="search_audit",
                    reason="pipecat client shape moved - searches will "
                    "be invisible again",
                )
        stages = [user_agg, llm]
        busy = busy_stage(voice, log, self.toolkit, gate)
        if busy is not None:
            stages.append(busy)
        return stages + [_make_tts(voice, secrets), transport.output(), asst_agg]

    def _save_and_carry(self):
        """Save the transcript and carry the last turns."""
        if self.context is None:
            return
        msgs = list(self.context.messages)
        traces.save("voice", msgs, {"provider": self.provider, "dry_run": self.dry_run})
        carry.save(msgs, self.toolkit)
