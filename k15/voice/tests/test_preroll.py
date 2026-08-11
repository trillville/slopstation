"""Blind test: the wake pre-roll path (assumptions row 10).

Part 1 (pure): WakeCapture pump/stop semantics against a fake stream -
seed ring + pumped chunks in order, idempotent stop, stream closed, device
death mid-pump keeps what we have, runaway cap.

Part 2 (real devices, like test_session_pipeline): a running PipelineWorker
with [transport.input(), PrerollFeeder, collector] proves the ordering
contract the feature rests on: StartFrame first, then the ENTIRE pre-roll,
only then live mic audio - no interleave, no loss. Run:
    .venv\\Scripts\\python tests\\test_preroll.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from preroll import CHUNK_BYTES, PrerollFeeder, WakeCapture


class FakeStream:
    """PyAudio-shaped: read(n_samples) returns n*2 bytes, chunk k patterned."""

    def __init__(self, fail_after=None):
        self.n = 0
        self.fail_after = fail_after
        self.closed = False

    def read(self, n, exception_on_overflow=True):
        if self.fail_after is not None and self.n >= self.fail_after:
            raise OSError(-9999, "device gone")
        self.n += 1
        time.sleep(0.005)
        return bytes([self.n % 256]) * (n * 2)

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def test_capture_orders_and_stops():
    seed = [b"S" * CHUNK_BYTES, b"E" * CHUNK_BYTES]
    stream = FakeStream()
    cap = WakeCapture(stream, seed)
    time.sleep(0.08)                    # let the pump add a few chunks
    pcm = cap.stop()
    assert pcm.startswith(seed[0] + seed[1]), "seed ring must lead the pcm"
    assert len(pcm) > 2 * CHUNK_BYTES, "pump added nothing"
    # Pumped chunks arrive in read order right after the seed.
    assert pcm[2 * CHUNK_BYTES:3 * CHUNK_BYTES] == bytes([1]) * CHUNK_BYTES
    assert pcm[3 * CHUNK_BYTES:4 * CHUNK_BYTES] == bytes([2]) * CHUNK_BYTES
    assert stream.closed, "stop() must close the wake stream"
    assert cap.stop() is pcm, "stop() must be idempotent"
    print(f"OK - capture: seed+{len(pcm) // CHUNK_BYTES - 2} pumped chunks "
          f"in order, idempotent stop, stream closed")


def test_capture_survives_device_death():
    stream = FakeStream(fail_after=3)
    cap = WakeCapture(stream, [b"S" * CHUNK_BYTES])
    time.sleep(0.08)                    # pump hits the OSError and exits
    pcm = cap.stop()
    assert len(pcm) == 4 * CHUNK_BYTES, f"want seed+3 chunks, got {len(pcm)}"
    print("OK - capture: device death mid-pump keeps captured audio")


def test_capture_runaway_cap():
    class Capped(WakeCapture):
        MAX_S = 1                       # 12 chunks at 80 ms

    cap = Capped(FakeStream(), [b"S" * CHUNK_BYTES] * 2)
    time.sleep(0.25)                    # far longer than the cap needs
    pcm = cap.stop()
    assert len(pcm) == 12 * CHUNK_BYTES, f"cap leaked: {len(pcm) // CHUNK_BYTES} chunks"
    print("OK - capture: runaway cap stops the pump at MAX_S")


def test_feeder_chunking():
    feeder = PrerollFeeder(lambda m: None)
    feeder.pcm = b"\xaa" * (CHUNK_BYTES * 2 + 100)
    frames = feeder._frames()
    assert [len(f.audio) for f in frames] == [CHUNK_BYTES, CHUNK_BYTES, 100]
    assert all(f.sample_rate == 16000 and f.num_channels == 1 for f in frames)
    assert b"".join(f.audio for f in frames) == feeder.pcm
    feeder.pcm = b""
    assert feeder._frames() == []
    print("OK - feeder: chunking preserves bytes, empty pcm feeds nothing")


async def test_pipeline_ordering():
    """The load-bearing claim: a live worker delivers StartFrame, then the
    whole pre-roll, then live mic frames - strictly in that order."""
    from pipecat.frames.frames import InputAudioRawFrame, StartFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.transports.local.audio import (LocalAudioTransport,
                                                LocalAudioTransportParams)
    from pipecat.workers.runner import WorkerRunner

    class Collector(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.events = []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, StartFrame):
                self.events.append(("start", b""))
            elif isinstance(frame, InputAudioRawFrame):
                self.events.append(("audio", frame.audio))
            await self.push_frame(frame, direction)

    marker = b"".join(bytes([0xA0 + i]) * CHUNK_BYTES for i in range(4))
    feeder = PrerollFeeder(lambda m: None)
    feeder.pcm = marker
    collector = Collector()

    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_in_sample_rate=16000,
        audio_out_enabled=True, audio_out_sample_rate=16000,
    ))
    worker = PipelineWorker(
        Pipeline([transport.input(), feeder, collector, transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000,
                              audio_out_sample_rate=16000),
        enable_rtvi=False,
        idle_timeout_secs=20,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    await asyncio.sleep(1.5)            # pipeline up + live mic flowing
    await worker.cancel(reason="test done")
    await asyncio.wait_for(run_task, timeout=10)

    kinds = [k for k, _ in collector.events]
    assert kinds and kinds[0] == "start", f"first event was {kinds[:3]}"
    audio = [a for k, a in collector.events if k == "audio"]
    assert len(audio) > 4, "no live mic frames followed the pre-roll"
    assert b"".join(audio[:4]) == marker, \
        "pre-roll must arrive complete and first, before any live mic frame"
    assert audio[4][:1] != b"\xa0", "live frames must not repeat the pre-roll"
    print(f"OK - pipeline: StartFrame -> 4 pre-roll chunks -> "
          f"{len(audio) - 4} live mic frames, strictly ordered")


def main():
    test_capture_orders_and_stops()
    test_capture_survives_device_death()
    test_capture_runaway_cap()
    test_feeder_chunking()
    asyncio.run(test_pipeline_ordering())
    print("OK - pre-roll: capture, chunking, and pipeline ordering all hold")


if __name__ == "__main__":
    main()
