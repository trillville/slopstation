"""Build and run one Pipecat voice session."""

import time
from typing import Any

from slopstation import config, logbook
from slopstation.agent.speech import keyterms
from slopstation.agent.telemetry import sentry, traces
from slopstation.agent.tools import library, titles

log = logbook.logger("voice")


# Cross-session context: the last turns, and the tools find_tools loaded,
# so a follow-up session keeps what the last one found.
CARRY: dict[str, Any] = {"messages": [], "loaded": [], "gate": None, "t": 0.0}


def _trim_carry(messages):
    """Keep only complete tool exchanges beginning with a user message."""
    msgs = list(messages)
    while msgs and msgs[0].get("role") != "user":
        msgs.pop(0)
    if msgs and msgs[-1].get("tool_calls"):
        msgs.pop()
    return msgs


def busy_stage(voice, log, toolkit):
    """The busy tone, between the model and the speech, or None when the
    config turns it off. A tool call still out after busyAfterMs gets one
    acknowledgment per turn: the tool's own phrase from its spec, else
    busyPhrase, else the busy earcon. busyEnabled false, or busyAfterMs 0,
    is off."""
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
    )


def _make_tts(voice, secrets):
    from pipecat.services.deepgram.tts import DeepgramTTSService

    return DeepgramTTSService(
        api_key=secrets["deepgramApiKey"],
        sample_rate=16000,
        settings=DeepgramTTSService.Settings(voice=voice["ttsVoice"]),
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

        # reasoning must be the typed config, not a dict: pipecat accepts
        # anything at construction, then calls .model_dump() at inference.
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
    """Pipecat logs its own errors to loguru; a transport write failure pushes
    no frame, so a dead speaker otherwise reads as a clean session."""

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
                filter=lambda r: r["name"].startswith("pipecat."),
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
        cfg,
        secrets,
        matcher,
        dry_run,
        input_idx,
        output_idx,
        capture=None,
        operations=None,
        ack=None,
        steam=None,
        media=None,
        on_end_session=None,
        room=None,
    ):
        self.cfg, self.secrets, self.matcher = cfg, secrets, matcher
        self.dry_run = dry_run
        self.input_idx, self.output_idx = input_idx, output_idx
        self.capture = capture
        self.operations, self.ack, self.steam = operations, ack, steam
        self.media = media
        self.on_end_session = on_end_session  # the room ducker's restore
        self.room = room  # voice.RoomState, or None when ducking is off
        self.voice = cfg["voice"]
        self.provider = self.voice["assistantProvider"]
        self.context = None  # the LLM lane's, once built
        self.toolkit = None  # the LLM lane's tools, once built
        self.audio_failed = False  # the speaker stopped taking frames

    async def run(self):
        from pipecat.frames.frames import (
            BotSpeakingFrame,
            InterimTranscriptionFrame,
            ProposedUserStartedSpeakingFrame,
            TranscriptionFrame,
            UserStartedSpeakingFrame,
        )
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        from pipecat.transports.local.audio import (
            LocalAudioTransport,
            LocalAudioTransportParams,
        )
        from pipecat.turns.user_turn_processor import UserTurnProcessor
        from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
        from pipecat.workers.runner import WorkerRunner

        from slopstation.agent.dispatch import Dispatch
        from slopstation.agent.llm.assistant import PROVIDER_KEY
        from slopstation.agent.speech.audio import wake_phrase as _wake_phrase
        from slopstation.agent.speech.grammar_gate import GrammarGate
        from slopstation.agent.speech.level import RoomLevel
        from slopstation.agent.speech.preroll import PrerollFeeder

        cfg, secrets, voice = self.cfg, self.secrets, self.voice
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
                # Flux's cap on a turn after speech stops; unset is its own 5 s.
                eot_timeout_ms=(
                    int(voice["eotTimeoutMs"]) if voice.get("eotTimeoutMs") else None
                ),
            ),
        )

        # Floor 0 (a config from before the key) measures and never mutes.
        loud = (lambda: self.room.loud) if self.room is not None else None
        level = RoomLevel(
            floor_db=float(voice.get("chatterFloorDb", 0) or 0), log=log, loud=loud
        )
        dispatcher = Dispatch(
            cfg, log, dry_run=self.dry_run, on_end_session=self.on_end_session
        )
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
            # The duck runs off-thread; read it per turn, not at build.
            loud=loud,
            level=level,
        )

        feeder = PrerollFeeder(log)
        # Flux only PROPOSES turn edges since pipecat 1.8, and its stop
        # proposal is a queued ControlFrame - resolved downstream of a gate
        # whose queue blocks on dispatch, a stale stop can land after the next
        # turn's start. enable_interruptions=True cuts the answer on talk-over;
        # False lets it play through.
        turns = UserTurnProcessor(
            user_turn_strategies=ExternalUserTurnStrategies(enable_interruptions=True)
        )
        stages = [transport.input(), feeder, level, stt, turns, gate]
        if assistant_live:
            stages += self._assistant_stages(transport, dispatcher, gate)
        else:
            stages += [transport.output()]

        # enable_metrics is what populates token counts and time to first
        # byte in the spans;
        # enable_tracing belongs on PipelineWorker, not PipelineTask.
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
            # Pipecat's conversation id IS our session id: a Sentry
            # Conversation and the JSONL lines around it join on this value.
            conversation_id=sentry.conversation_id() if tracing_on else None,
            additional_span_attributes=sentry.span_attributes() if tracing_on else None,
            idle_timeout_secs=voice["holdWindowS"],
            # The real start frame comes from the turns resolver; the proposal
            # is Flux's own push and resets the clock even if that wiring
            # moves.
            idle_timeout_frames=(
                TranscriptionFrame,
                InterimTranscriptionFrame,
                UserStartedSpeakingFrame,
                ProposedUserStartedSpeakingFrame,
                BotSpeakingFrame,
            ),
            cancel_on_idle_timeout=False,  # we decide - see the handler
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

        # A setup exception (device open, Flux connect - both run in setup
        # under 1.8) is swallowed by the runner's gather(return_exceptions);
        # without this flag a failed build reads as a clean instant close and
        # session_crashed never fires.
        started = False

        @worker.event_handler("on_pipeline_started")
        async def _on_started(worker, frame):
            nonlocal started
            started = True

        runner = WorkerRunner(handle_sigint=False)
        # Handed over LIVE, stopped by the feeder at StartFrame: 1.8 runs the
        # Flux connect during setup, before StartFrame starts the mic, so a
        # capture stopped here would lose that window (0.3-1.5 s of speech).
        # The can't-tell chime deadline must not ride into that extra window -
        # it counts from the wake, and a one-breath command is still mid-word
        # at 1.5 s.
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
            # pipecat (still in 1.8.1) never terminates the PyAudio handle it
            # creates and exposes no public cleanup; a fresh transport per wake
            # would leak one each time. Guarded so an upstream rename logs, not
            # crashes.
            pa = getattr(transport, "_pyaudio", None)
            if pa is not None:
                try:
                    pa.terminate()
                except Exception as e:
                    log.warn("pyaudio_terminate_failed", err=str(e))
        self.audio_failed = errors.output > 0
        self._save_and_carry()

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
            Toolkit,
            server_tools,
            system_instruction,
        )

        voice, secrets = self.voice, self.secrets
        carrying = time.time() - CARRY["t"] < voice["followupCarryS"]
        carry = list(CARRY["messages"]) if carrying else []
        # Native (provider-executed) tools ride custom_tools. Only the OpenAI
        # adapter has that passthrough (AdapterType still has no ANTHROPIC in
        # 1.8.1), so the knob is a no-op under the anthropic provider.
        native = server_tools(voice, "openai") if self.provider == "openai" else []

        def tools_schema():
            assert self.toolkit is not None
            return ToolsSchema(
                # -> one tool_call event per call
                standard_tools=self.toolkit.function_schemas(log),
                custom_tools={AdapterType.OPENAI: native} if native else None,
            )

        # Built after the gate so its request_stop can ride here. When
        # find_tools loads more, the context's tool list is replaced; Pipecat
        # re-reads it on the next inference and registers the new handlers.
        # on_load runs on the tool's worker thread (asyncio.to_thread), not
        # the loop: set_tools is one attribute assignment, so that is safe,
        # and the loop only reads the list after the tool result lands.
        self.toolkit = Toolkit(
            dispatcher,
            log,
            operations=self.operations,
            on_stop_listening=gate.request_stop,
            voice=voice,
            steam=self.steam,
            media=self.media,
            on_load=lambda: (
                self.context.set_tools(tools_schema())
                if self.context is not None
                else None
            ),
            # A follow-up keeps the previous session's asks armed: a "yes" to
            # a question the last session ended on lands here, inside the
            # gate's own lifetime.
            gate=CARRY["gate"] if carrying else None,
        )
        if carrying:
            self.toolkit.load(CARRY["loaded"])
        self.context = LLMContext(messages=carry, tools=tools_schema())
        # Strategies passed explicitly so the aggregator does not build its
        # default per-session smart-turn ONNX model; turn resolution itself
        # happens upstream in the turns resolver (see run()), so these only
        # adopt already-real frames. enable_interruptions matches the
        # resolver's.
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
            # Pipecat (still in 1.8.1) has no handling for provider-executed
            # tools: a web_search never reaches the context, so the model
            # cannot tell that it searched.
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
        busy = busy_stage(voice, log, self.toolkit)
        if busy is not None:
            stages.append(busy)
        return stages + [_make_tts(voice, secrets), transport.output(), asst_agg]

    def _save_and_carry(self):
        """Dump the transcript and retain the last complete turns."""
        if self.context is None:
            return
        msgs = list(self.context.messages)
        traces.save("voice", msgs, {"provider": self.provider, "dry_run": self.dry_run})
        CARRY["messages"] = _trim_carry(msgs[-8:])
        CARRY["loaded"] = list(self.toolkit.loaded) if self.toolkit else []
        CARRY["gate"] = self.toolkit.ctx.gate if self.toolkit else None
        CARRY["t"] = time.time()
