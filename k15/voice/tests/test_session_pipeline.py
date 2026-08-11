"""Blind test: a REAL session pipeline end-to-end minus STT -
LocalAudioTransport on real devices, GrammarGate, dry-run dispatch, earcons
through the actual speaker (wake chime included, plus the fold rule that keeps
an instant success from doubling it), and the exit phrase ending the worker.
Scripted TranscriptionFrames stand in for Flux (its own connect path needs the
key). Brief tones will be audible. Run:
    .venv\\Scripts\\python tests\\test_session_pipeline.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cglib
from dispatch import Dispatch
from grammar_gate import GrammarGate, GrammarMatcher
from preroll import WakeAck

SCRIPT = [
    ("volume up", "VolumeUp -> dry-run"),
    ("switch to the apple tv", "SwitchInput -> dry-run"),
    ("start a session", "StartSession -> dry-run"),
    ("what mech games do i have", "passing to assistant"),
    ("thanks", "exit phrase"),
]


async def run():
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.local.audio import (LocalAudioTransport,
                                                LocalAudioTransportParams)
    from pipecat.workers.runner import WorkerRunner

    lines = []

    def log(msg):
        print(f"[log] {msg}")
        lines.append(msg)

    cfg = cglib.load_config()
    # assistant_enabled without an LLM stage: the no-match line exercises the
    # REAL handoff (the in-flight flag that defers the idle timeout, set
    # inside a live pipeline); the transcript just dead-ends at the output
    # transport.
    gate = GrammarGate(GrammarMatcher(cfg["voice"]),
                       Dispatch(cfg, log, dry_run=True), log,
                       assistant_enabled=True, ack=WakeAck())
    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_in_sample_rate=16000,
        audio_out_enabled=True, audio_out_sample_rate=16000,
    ))
    worker = PipelineWorker(
        Pipeline([transport.input(), gate, transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000,
                              audio_out_sample_rate=16000),
        enable_rtvi=False,
        idle_timeout_secs=20,          # backstop only; the exit phrase ends it
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    await asyncio.sleep(1.5)           # pipeline up
    for text, _ in SCRIPT[:-1]:
        await worker.queue_frame(TranscriptionFrame(
            text=text, user_id="test", timestamp="t"))
        await asyncio.sleep(0.9)       # let earcons play

    # The no-match handoff above left a real answer "in flight". Error
    # honesty: an ErrorFrame must clear the flag (which is what pins the
    # idle handler open) and play the fail earcon instead of silence.
    from pipecat.frames.frames import ErrorFrame
    assert gate._assistant_pending, "handoff must mark an answer in flight"
    await worker.queue_frame(ErrorFrame(error="bench: synthetic LLM failure"))
    await asyncio.sleep(0.9)
    assert gate._assistant_pending == 0.0, "ErrorFrame must clear in-flight"

    text, _ = SCRIPT[-1]               # "thanks" ends the worker
    await worker.queue_frame(TranscriptionFrame(
        text=text, user_id="test", timestamp="t"))

    await asyncio.wait_for(run_task, timeout=10)   # exit phrase must end it

    missing = [want for _, want in SCRIPT
               if not any(want in l for l in lines)]
    assert not missing, f"missing log evidence: {missing}"
    assert any("pipeline error" in l for l in lines)
    # The lock arbiter ran for real (no lock on this machine = launchable).
    assert any("couch.py start" in l for l in lines)
    # The wake chime is claimed by the FIRST transcript, and only that first
    # command's success is close enough to fold into it - the later ones are
    # 0.9 s behind (past ACK_COALESCE_S) and must ack normally, or a real
    # session would go silent on everything after the wake.
    folded = sum("folded into the wake chime" in l for l in lines)
    assert folded == 1, f"{folded} acks folded, want exactly the first"
    print("OK - session pipeline: gate matched/dry-dispatched/acked, first ok "
          "folded into the wake chime, error cleared the in-flight flag, "
          "exit phrase ended the worker cleanly")


if __name__ == "__main__":
    asyncio.run(run())
