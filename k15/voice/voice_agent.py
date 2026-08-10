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
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                    # noqa: E402
import earcons                                  # noqa: E402
from dispatch import Dispatch                   # noqa: E402
from grammar_gate import GrammarGate, GrammarMatcher   # noqa: E402

SECRETS = HERE.parent / "secrets.json"
LIBRARY = HERE.parent / "state" / "library.json"

log = cglib.make_log("voice")


# --- config / secrets ---------------------------------------------------------

def load_secrets():
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except OSError:
        return {}


def real_key(value):
    """Placeholder discipline (assumption #1): template junk disables a lane
    with a message, never a crash."""
    return (isinstance(value, str) and "..." not in value
            and not value.upper().startswith("PLACEHOLDER")
            and len(value.strip()) >= 15)


def load_titles(count):
    """Installed titles by recency from the library index (C2 writes it).
    Feeds Flux keyterms, the grammar's {game} slot, and fuzzy resolution."""
    try:
        rows = json.loads(LIBRARY.read_text(encoding="utf-8"))["installed"]
    except (OSError, KeyError, ValueError):
        return None
    rows = sorted(rows, key=lambda r: r.get("lastPlayed", 0), reverse=True)
    return [r["name"] for r in rows if r.get("name")][:count]


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
    """Blocking playback for the wake tick (the pipeline isn't up yet)."""
    s = pa.open(format=8, channels=1, rate=earcons.SAMPLE_RATE,  # 8 = paInt16
                output=True, output_device_index=device_index)
    try:
        s.write(pcm)
    finally:
        s.stop_stream()
        s.close()


class WakeListener:
    """openWakeWord over a raw PyAudio stream. Owns the mic while DORMANT;
    releases it before a session pipeline opens it."""

    CHUNK = 1280                                # oWW's native 80 ms hop

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

    def wait_for_wake(self, threshold, on_score=None):
        """Blocks until the wake word fires; returns the score. The stream is
        closed before returning so the session pipeline can own the device."""
        stream = self.pa.open(format=8, channels=1, rate=16000, input=True,
                              frames_per_buffer=self.CHUNK,
                              input_device_index=self.device_index)
        try:
            while True:
                data = stream.read(self.CHUNK, exception_on_overflow=False)
                score = self.score_chunk(self.np.frombuffer(data, self.np.int16))
                if on_score:
                    on_score(score)
                if score >= threshold:
                    self.model.reset()
                    return score
        finally:
            stream.stop_stream()
            stream.close()


# --- the per-session pipeline -------------------------------------------------

CARRY = {"messages": [], "t": 0.0}      # cross-session context (followupCarryS)


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


async def run_session(cfg, secrets, args, input_idx, output_idx):
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
    titles = load_titles(voice["keytermCount"])

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
            **({"keyterm": titles} if titles else {}),
        ),
    )

    import titles as titles_mod
    dispatcher = Dispatch(cfg, log, dry_run=args.dry_run)
    assistant_live = real_key(secrets.get("anthropicApiKey"))
    gate = GrammarGate(
        GrammarMatcher(voice, titles),
        dispatcher,
        log,
        resolve_game=(titles_mod.build_resolver(voice["fuzzyTitleThreshold"])
                      if titles else None),
        assistant_enabled=assistant_live,
    )

    stages = [transport.input(), stt, gate]
    context = None
    if assistant_live:
        from assistant import (function_schemas, system_instruction,
                               tool_impls)
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair)
        from pipecat.services.anthropic.llm import AnthropicLLMService

        carry = (list(CARRY["messages"])
                 if time.time() - CARRY["t"] < voice["followupCarryS"] else [])
        context = LLMContext(
            messages=carry,
            tools=function_schemas(tool_impls(dispatcher, log)))
        user_agg, asst_agg = LLMContextAggregatorPair(context)
        llm = AnthropicLLMService(
            api_key=secrets["anthropicApiKey"],
            settings=AnthropicLLMService.Settings(
                model=voice["assistantModel"],
                system_instruction=system_instruction(),
                enable_prompt_caching=True,
                max_tokens=400))
        stages += [user_agg, llm, _make_tts(voice, secrets),
                   transport.output(), asst_agg]
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
        cancel_on_idle_timeout=True,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()
    if context is not None:                     # cross-session follow-ups
        CARRY["messages"] = list(context.messages)[-8:]
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
    args = ap.parse_args()

    if args.devices:
        from spike import list_devices
        list_devices()
        return 0

    if args.text:
        cfg = cglib.load_config()
        secrets = load_secrets()
        if not real_key(secrets.get("anthropicApiKey")):
            print("anthropicApiKey is a placeholder - the REPL needs a real key")
            return 1
        from assistant import repl
        return repl(cfg, secrets, log, dry_run=True)

    import pyaudio
    cfg = cglib.load_config()
    voice = cfg["voice"]
    secrets = load_secrets()
    cglib.rotate_log()

    pa = pyaudio.PyAudio()
    input_idx = resolve_device(pa, voice["inputDeviceName"], want_input=True)
    output_idx = resolve_device(pa, voice["outputDeviceName"], want_input=False)

    stt_live = real_key(secrets.get("deepgramApiKey"))
    if not stt_live:
        log("deepgramApiKey is a placeholder - wake word runs, but sessions "
            "are DISABLED until a real key lands in secrets.json")

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
        return 0

    if args.false_accept_soak:
        log("false-accept soak: leave the room noisy; every wake is a false accept.")
        t0, n = time.time(), 0
        while True:
            listener.wait_for_wake(voice["wakeThreshold"])
            n += 1
            hrs = (time.time() - t0) / 3600
            log(f"FALSE ACCEPT #{n} after {hrs:.2f}h ({n / max(hrs, 0.01):.1f}/hr)")
            time.sleep(1.0)
        return 0

    while True:
        score = listener.wait_for_wake(voice["wakeThreshold"])
        log(f"wake (score {score:.2f})")
        if not stt_live:
            play_pcm(pa, earcons.pcm("fail"), output_idx)
            continue
        play_pcm(pa, earcons.pcm("wake"), output_idx)
        log("session open")
        try:
            asyncio.run(run_session(cfg, secrets, args, input_idx, output_idx))
        except Exception as e:
            log(f"session crashed: {e!r} - back to dormant")
        log("session closed - dormant")
        if args.once:
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("voice agent stopped (Ctrl+C)")
