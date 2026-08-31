"""Blind test: a REAL session pipeline end-to-end minus STT -
LocalAudioTransport on real devices, GrammarGate, dry-run dispatch, earcons
through the actual speaker, and the exit phrase ending the worker. Scripted
TranscriptionFrames stand in for Flux, whose connect path needs the key.
Brief tones will be audible. Run:
    .venv\\Scripts\\python tests\\test_session_pipeline.py
"""
import asyncio

import _bootstrap  # noqa: F401

import cglib
from agent.brain.dispatch import Dispatch
from agent.speech.grammar_gate import GrammarGate, GrammarMatcher
from agent.speech.preroll import WakeAck

# (utterance, event the gate must emit, intent where the event carries one).
SCRIPT = [
    ("volume up", "dispatch", "VolumeUp"),
    ("switch to the apple tv", "dispatch", "SwitchInput"),
    ("start a session", "dispatch", "StartSession"),
    ("what mech games do i have", "gate_miss", None),
    ("thanks", "session_exit_phrase", None),
]


async def run():
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.local.audio import (LocalAudioTransport,
                                                LocalAudioTransportParams)
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
    from pipecat.workers.runner import WorkerRunner

    log = cglib.CapturingLog("voice", echo=True)

    cfg = cglib.load_config()
    # assistant_enabled with no LLM stage: the no-match line exercises the
    # real handoff, then dead-ends at the output transport.
    gate = GrammarGate(GrammarMatcher(cfg["voice"]),
                       Dispatch(cfg, log, dry_run=True), log,
                       assistant_enabled=True, ack=WakeAck())
    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_in_sample_rate=16000,
        audio_out_enabled=True, audio_out_sample_rate=16000,
    ))
    # The turns resolver rides in the same seat as production (between STT
    # and the gate); scripted transcripts pass through it untouched.
    turns = UserTurnProcessor(
        user_turn_strategies=ExternalUserTurnStrategies(
            enable_interruptions=True))
    worker = PipelineWorker(
        Pipeline([transport.input(), turns, gate, transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000,
                              audio_out_sample_rate=16000),
        enable_rtvi=False,
        idle_timeout_secs=20,          # backstop only; the exit phrase ends it
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    await asyncio.sleep(1.5)           # pipeline up
    for text, _, _ in SCRIPT[:-1]:
        await worker.queue_frame(TranscriptionFrame(
            text=text, user_id="test", timestamp="t"))
        await asyncio.sleep(0.9)       # let earcons play

    # ErrorFrame must clear the in-flight flag that pins the idle handler.
    from pipecat.frames.frames import ErrorFrame
    assert gate._assistant_pending, "handoff must mark an answer in flight"
    await worker.queue_frame(ErrorFrame(error="bench: synthetic LLM failure"))
    await asyncio.sleep(0.9)
    assert gate._assistant_pending == 0.0, "ErrorFrame must clear in-flight"

    text = SCRIPT[-1][0]               # "thanks" ends the worker
    await worker.queue_frame(TranscriptionFrame(
        text=text, user_id="test", timestamp="t"))

    await asyncio.wait_for(run_task, timeout=10)   # exit phrase must end it

    missing = [f"{ev}({intent or ''})" for _, ev, intent in SCRIPT
               if not any(r["event"] == ev
                          and (intent is None or r.get("intent") == intent)
                          for r in log.records)]
    assert not missing, f"missing event evidence: {missing}\ngot {log.events()}"
    assert log.find("pipeline_error"), log.events()
    # The lock arbiter ran for real (no lock on this machine = launchable).
    assert any("couch.py start" in r.get("action", "")
               for r in log.find("dry_run_would")), log.records
    # The first transcript claims the wake chime and folds into it; the rest
    # are 0.9 s behind, past ACK_COALESCE_S, so they ack normally.
    folded = len(log.find("earcon_folded"))
    assert folded == 1, f"{folded} acks folded, want exactly the first"
    print("OK - session pipeline: gate matched/dry-dispatched/acked, first ok "
          "folded into the wake chime, error cleared the in-flight flag, "
          "exit phrase ended the worker cleanly")


if __name__ == "__main__":
    _bootstrap.wants("audio")
    asyncio.run(run())
