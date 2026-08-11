"""K15 voice agent: wake word -> session pipeline -> dispatch.

Architecture (see docs/voice-control-design.md + voice-assumptions.md #7):
the wake loop runs OUTSIDE Pipecat (raw PyAudio + openWakeWord, zero cloud);
each wake builds and runs ONE PipelineWorker (mic -> Flux STT -> GrammarGate
-> speaker) that lives for the session and is torn down at its end - fresh
Flux socket per session, $0 idle by construction. Sessions end on an exit
phrase or idle timeout (holdWindowS).

Modes:
  (default)             run the agent
  --devices             list audio devices and exit
  --dry-run             full pipeline; side effects logged, not executed
  --wake-trials         log wake detections + confidences; never start sessions
  --false-accept-soak   count spurious wakes over hours; never start sessions
  --once                exactly one session, then exit (bench)

Overlay rule: this process is never load-bearing - the chord listener is a
separate process on system python and must survive anything that happens here.
"""
import argparse
import asyncio
import collections
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                    # noqa: E402
import earcons                                  # noqa: E402
import library                                  # noqa: E402
import titles                                   # noqa: E402
from dispatch import Dispatch                   # noqa: E402
from grammar_gate import GrammarGate, GrammarMatcher   # noqa: E402
from preroll import PrerollFeeder, WakeCapture  # noqa: E402  (pipecat frames
# are already loaded via grammar_gate, so this adds no startup cost)

log = cglib.make_log("voice")

# config.json's voice section is the one home for tuning values; a deployed
# config missing any of these should fail loudly at startup, not per-wake.
REQUIRED_VOICE = ("wakeModel", "wakeThreshold", "holdWindowS", "followupCarryS",
                  "eotThreshold", "eagerEotThreshold", "keytermCount",
                  "fuzzyTitleThreshold", "volumeStep", "volumeMax", "ttsVoice",
                  "assistantProvider", "assistantModel", "inputs")


def load_titles(count):
    """Installed titles by recency: Flux keyterms + fuzzy-resolution corpus."""
    rows = library.load().get("installed", [])
    rows.sort(key=lambda r: r.get("lastPlayed", 0), reverse=True)
    return [r["name"] for r in rows if r.get("name")][:count]


def refresh_library_bg():
    """Full catalog sync off the wake loop - a slow/asleep PC (30 s ssh timeout)
    or a metadata crawl must never delay a wake. library.sync fail-softs, is
    key-gated per layer, and no-ops if one is already running."""
    threading.Thread(target=library.sync, daemon=True).start()


def prewarm_imports_bg(provider):
    """First-wake latency fix: pipecat's service modules + the provider SDK
    take several seconds to import on the K15's U-class CPU, which showed up
    as ~6.5 s of wake-tick-to-listening dead air on the first session. Import
    them at boot on a background thread so the first session builds as fast
    as every later one (imports are idempotent and lock-protected)."""
    def warm():
        import pipecat.processors.aggregators.llm_response_universal  # noqa: F401
        import pipecat.services.deepgram.flux.stt   # noqa: F401
        import pipecat.services.deepgram.tts        # noqa: F401
        import pipecat.transports.local.audio       # noqa: F401
        if provider == "openai":
            import pipecat.services.openai.responses.llm  # noqa: F401
        else:
            import pipecat.services.anthropic.llm   # noqa: F401
    threading.Thread(target=warm, daemon=True).start()


# --- audio plumbing outside the pipeline --------------------------------------

def resolve_device(pa, fragment, want_input):
    """Config name-fragment -> PyAudio device index; None = system default."""
    if not fragment:
        return None
    frag = fragment.lower()
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        channels = d["maxInputChannels"] if want_input else d["maxOutputChannels"]
        if channels and frag in d["name"].lower():
            return i
    log(f"WARNING: no {'input' if want_input else 'output'} device matching "
        f"'{fragment}' - using system default")
    return None


def play_pcm(pa, pcm, device_index=None):
    """Blocking playback for the wake tick (the pipeline isn't up yet).
    One retry after a settle: Bluetooth outputs (AirPods) renegotiate
    profiles around our stream churn and can transiently refuse to open
    (-9999). A missed tick must never take the agent down either way."""
    import pyaudio
    for attempt in (1, 2):
        try:
            s = pa.open(format=pyaudio.paInt16, channels=1,
                        rate=earcons.SAMPLE_RATE, output=True,
                        output_device_index=device_index)
            try:
                s.write(pcm)
            finally:
                s.stop_stream()
                s.close()
            return
        except OSError as e:
            if attempt == 1:
                time.sleep(0.5)
            else:
                log(f"earcon playback failed ({e}) - continuing without it")


def close_stream_quietly(stream):
    """Best-effort close for a possibly-dead stream: after a -9999 host error
    (BT profile flap, device yanked) the stream is already torn down and
    stop/close themselves raise 'Stream not open' - the original error is the
    story, cleanup must never replace it (crashed the agent live 2026-08-10)."""
    for op in (stream.stop_stream, stream.close):
        try:
            op()
        except OSError:
            pass


class WakeListener:
    """openWakeWord over a raw PyAudio stream. Owns the mic while DORMANT;
    releases it before a session pipeline opens it."""

    CHUNK = 1280                                # oWW's native 80 ms hop
    PREROLL_CHUNKS = 25                         # 2 s ring kept ahead of detection

    def __init__(self, pa, voice_cfg, input_device_index):
        import numpy as np
        from openwakeword.model import Model
        self.np = np
        self.pa = pa
        self.device_index = input_device_index
        self.model_name = voice_cfg["wakeModel"]          # e.g. hey_jarvis_v0.1
        self.key = self.model_name.rsplit("_v", 1)[0]     # e.g. hey_jarvis
        self._ensure_model()
        self.model = Model(wakeword_models=[self.key], inference_framework="onnx")

    def _ensure_model(self):
        import openwakeword
        from openwakeword.utils import download_models
        res = Path(openwakeword.__file__).parent / "resources" / "models"
        if not (res / f"{self.model_name}.onnx").exists():
            log(f"wake model {self.model_name} missing - downloading once")
            download_models([self.key])

    def score_chunk(self, chunk_int16):
        scores = self.model.predict(chunk_int16)
        return max(scores.values())

    def _open_stream(self):
        import pyaudio
        return self.pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                            input=True, frames_per_buffer=self.CHUNK,
                            input_device_index=self.device_index)

    def _listen(self, stream, threshold, on_score, ring):
        while True:
            data = stream.read(self.CHUNK, exception_on_overflow=False)
            if ring is not None:
                ring.append(data)
            score = self.score_chunk(self.np.frombuffer(data, self.np.int16))
            if on_score:
                on_score(score)
            if score >= threshold:
                self.model.reset()
                return score

    def wait_for_wake(self, threshold, on_score=None):
        """Blocks until the wake word fires; returns the score. The stream is
        closed before returning (trials/soak modes - no session follows)."""
        stream = self._open_stream()
        try:
            return self._listen(stream, threshold, on_score, None)
        finally:
            close_stream_quietly(stream)

    def wait_for_wake_capture(self, threshold):
        """Blocks until the wake word fires; returns (score, WakeCapture). The
        stream is handed to the capture - NOT closed - so speech overlapping or
        right after the wake phrase ("hey jarvis volume up", no pause) survives
        the session build. The ring seeds the capture with the ~2 s before
        detection, wake phrase included; strip_wake removes it text-side.
        Caller must stop() the capture (idempotent) to release the mic."""
        stream = self._open_stream()
        ring = collections.deque(maxlen=self.PREROLL_CHUNKS)
        try:
            score = self._listen(stream, threshold, None, ring)
        except Exception:
            close_stream_quietly(stream)
            raise
        return score, WakeCapture(stream, ring)


# --- the per-session pipeline -------------------------------------------------

CARRY = {"messages": [], "t": 0.0}      # cross-session context (followupCarryS)


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
            log("TTS: Kokoro (local; first run downloads the model)")
            return KokoroTTSService()
        except Exception as e:
            log(f"Kokoro unavailable ({e}) - falling back to Aura-2")
    from pipecat.services.deepgram.tts import DeepgramTTSService
    return DeepgramTTSService(
        api_key=secrets["deepgramApiKey"], sample_rate=16000,
        settings=DeepgramTTSService.Settings(voice=voice["ttsVoice"]))


def _make_llm(voice, secrets, system_text):
    """The brain, provider-switchable via config.assistantProvider - so once the
    --text A/B picks a winner, production follows by flipping one config key.
    OpenAI uses the Responses API (reasoning + tools coexist there); effort is
    a config knob that trades latency for depth."""
    provider = voice.get("assistantProvider", "anthropic")
    if provider == "openai":
        from assistant import default_model
        from pipecat.services.openai.responses.llm import (
            OpenAIResponsesHttpLLMService)
        return OpenAIResponsesHttpLLMService(
            api_key=secrets["openaiApiKey"],
            settings=OpenAIResponsesHttpLLMService.Settings(
                model=default_model({"voice": voice}, "openai"),
                system_instruction=system_text, max_completion_tokens=1500,
                reasoning={"effort": voice.get("assistantReasoningEffort", "low")}))
    from pipecat.services.anthropic.llm import AnthropicLLMService
    return AnthropicLLMService(
        api_key=secrets["anthropicApiKey"],
        settings=AnthropicLLMService.Settings(
            model=voice["assistantModel"], system_instruction=system_text,
            enable_prompt_caching=True, max_tokens=400))


async def run_session(cfg, secrets, matcher, args, input_idx, output_idx,
                      capture=None):
    from pipecat.frames.frames import (BotSpeakingFrame,
                                       InterimTranscriptionFrame,
                                       TranscriptionFrame,
                                       UserStartedSpeakingFrame)
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
    from pipecat.transports.local.audio import (LocalAudioTransport,
                                                LocalAudioTransportParams)
    from pipecat.workers.runner import WorkerRunner

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
        mip_opt_out=True,                       # assumption #11: privacy > rate
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
    provider = voice.get("assistantProvider", "anthropic")
    assistant_live = cglib.real_key(secrets.get(BACKENDS[provider].key))
    gate = GrammarGate(
        matcher, dispatcher, log,
        resolve_game=(titles.build_resolver(voice["fuzzyTitleThreshold"])
                      if game_terms else None),
        assistant_enabled=assistant_live,
        wake_word=wake_phrase.split()[-1],      # "jarvis" - the strip anchor
    )

    feeder = PrerollFeeder(log)
    stages = [transport.input(), feeder, stt, gate]
    context = None
    if assistant_live:
        from assistant import function_schemas, system_instruction, tool_impls
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair)

        carry = (list(CARRY["messages"])
                 if time.time() - CARRY["t"] < voice["followupCarryS"] else [])
        context = LLMContext(
            messages=carry,
            tools=function_schemas(tool_impls(dispatcher, log)))
        user_agg, asst_agg = LLMContextAggregatorPair(context)
        stages += [user_agg, _make_llm(voice, secrets, system_instruction(cfg)),
                   _make_tts(voice, secrets), transport.output(), asst_agg]
    else:
        stages += [transport.output()]

    worker = PipelineWorker(
        Pipeline(stages),
        params=PipelineParams(audio_in_sample_rate=16000,
                              audio_out_sample_rate=16000),
        enable_rtvi=False,
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
            log("idle timeout deferred - user/dispatch still active")
            return
        log("idle - ending session")
        await worker.cancel(reason="idle")

    runner = WorkerRunner(handle_sigint=False)
    if capture is not None:
        # Stop as late as possible: every slow build step is behind us, only
        # worker start + the transport's mic-open remain, so the uncaptured
        # gap is ~100-200 ms instead of the whole session build.
        feeder.pcm = capture.stop()
    try:
        await runner.add_workers(worker)
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
                log(f"pyaudio terminate failed ({e}) - leaked one host handle")
    if context is not None:                     # cross-session follow-ups
        CARRY["messages"] = _trim_carry(list(context.messages)[-8:])
        CARRY["t"] = time.time()


# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--devices", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wake-trials", action="store_true")
    ap.add_argument("--false-accept-soak", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--text", action="store_true",
                    help="assistant REPL: typed transcripts, no audio; "
                         "always dry-run (actions log, never execute)")
    ap.add_argument("--provider", help="--text A/B: anthropic|openai")
    ap.add_argument("--model", help="--text A/B: model id override")
    ap.add_argument("--effort", help="--text A/B: openai reasoning effort "
                                     "(none|minimal|low|medium|high)")
    args = ap.parse_args()

    if args.devices:
        from spike import list_devices
        list_devices()
        return 0

    cfg = cglib.load_config()
    voice = cfg["voice"]
    missing = [k for k in REQUIRED_VOICE if k not in voice]
    if missing:
        log(f"config.json voice section missing keys: {missing} - fix and restart")
        return 1
    secrets = cglib.load_secrets()

    if args.text:
        from assistant import repl
        return repl(cfg, secrets, log, dry_run=True, provider=args.provider,
                    model=args.model, effort=args.effort)

    import pyaudio
    cglib.rotate_log()

    pa = pyaudio.PyAudio()
    input_idx = resolve_device(pa, voice["inputDeviceName"], want_input=True)
    output_idx = resolve_device(pa, voice["outputDeviceName"], want_input=False)

    stt_live = cglib.real_key(secrets.get("deepgramApiKey"))
    if not stt_live:
        log("deepgramApiKey is a placeholder - wake word runs, but sessions "
            "are DISABLED until a real key lands in secrets.json")

    # Build the grammar once (a YAML typo fails here, not per-wake); warm the
    # library index and the heavy pipeline imports in the background so the
    # first wake is as fast as every later one.
    matcher = GrammarMatcher(voice)
    refresh_library_bg()
    prewarm_imports_bg(voice.get("assistantProvider", "anthropic"))

    listener = WakeListener(pa, voice, input_idx)
    log(f"voice agent up - wake model {listener.model_name}, "
        f"threshold {voice['wakeThreshold']}"
        + (" [DRY-RUN]" if args.dry_run else ""))

    if args.wake_trials:
        log("wake-trials mode: say the wake word; every detection logs. Ctrl+C to stop.")
        n = 0
        while True:
            score = listener.wait_for_wake(voice["wakeThreshold"])
            n += 1
            log(f"wake #{n} (score {score:.2f})")
            play_pcm(pa, earcons.pcm("wake"), output_idx)
            time.sleep(1.0)                     # refractory: one hit per attempt

    if args.false_accept_soak:
        log("false-accept soak: leave the room noisy; every wake is a false accept.")
        t0, n = time.time(), 0
        while True:
            listener.wait_for_wake(voice["wakeThreshold"])
            n += 1
            hrs = (time.time() - t0) / 3600
            log(f"FALSE ACCEPT #{n} after {hrs:.2f}h ({n / max(hrs, 0.01):.1f}/hr)")
            time.sleep(1.0)

    while True:
        try:
            score, capture = listener.wait_for_wake_capture(voice["wakeThreshold"])
        except OSError as e:
            # Mic stream death mid-listen (BT profile flap, device yanked,
            # AirPods multipoint wandering off) must never kill the agent -
            # voice is not load-bearing. Breathe, then reopen fresh: the next
            # open binds whatever device is back.
            log(f"wake stream died ({e}) - reopening in 5s")
            time.sleep(5)
            continue
        log(f"wake (score {score:.2f})")
        if not stt_live:
            capture.stop()
            play_pcm(pa, earcons.pcm("fail"), output_idx)
            continue
        play_pcm(pa, earcons.pcm("wake"), output_idx)
        log("session open")
        try:
            asyncio.run(run_session(cfg, secrets, matcher, args,
                                    input_idx, output_idx, capture))
        except Exception as e:
            log(f"session crashed: {e!r} - back to dormant")
        finally:
            capture.stop()                      # idempotent; frees the mic if the build crashed
        refresh_library_bg()                    # pick up installs between sessions
        log("session closed - dormant")
        if args.once:
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("voice agent stopped (Ctrl+C)")
