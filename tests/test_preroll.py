"""Test wake pre-roll capture, acknowledgement, and pipeline ordering."""

import asyncio
import dataclasses
import threading
import time

import helpers
from helpers import CapturingLog
from slopstation.agent.speech.preroll import (
    CHUNK_BYTES,
    CHUNK_SAMPLES,
    SAMPLE_RATE,
    PrerollFeeder,
    WakeAck,
    WakeCapture,
)


@dataclasses.dataclass
class FakeStream:
    """PyAudio-shaped: read(n_samples) returns n*2 bytes, chunk k patterned."""

    fail_after: int | None = None
    n: int = 0
    closes: int = 0

    def read(self, n, exception_on_overflow=True):
        if self.fail_after is not None and self.n >= self.fail_after:
            raise OSError(-9999, "device gone")
        self.n += 1
        time.sleep(0.005)
        return bytes([self.n % 256]) * (n * 2)

    def stop_stream(self):
        pass

    def close(self):
        self.closes += 1


def test_capture_orders_and_stops():
    seed = [b"S" * CHUNK_BYTES, b"E" * CHUNK_BYTES]
    stream = FakeStream()
    cap = WakeCapture(stream, seed)
    time.sleep(0.08)  # let the pump add a few chunks
    pcm = cap.stop()
    assert pcm.startswith(seed[0] + seed[1]), "seed ring must lead the pcm"
    assert len(pcm) > 2 * CHUNK_BYTES, "pump added nothing"
    assert pcm[2 * CHUNK_BYTES : 3 * CHUNK_BYTES] == bytes([1]) * CHUNK_BYTES
    assert pcm[3 * CHUNK_BYTES : 4 * CHUNK_BYTES] == bytes([2]) * CHUNK_BYTES
    assert stream.closes, "stop() must close the wake stream"
    assert cap.stop() is pcm, "stop() must be idempotent"


def test_capture_survives_device_death():
    stream = FakeStream(fail_after=3)
    cap = WakeCapture(stream, [b"S" * CHUNK_BYTES])
    time.sleep(0.08)  # pump hits the OSError and exits
    pcm = cap.stop()
    assert len(pcm) == 4 * CHUNK_BYTES, f"want seed+3 chunks, got {len(pcm)}"


def test_capture_runaway_cap():
    class Capped(WakeCapture):
        MAX_S = 1  # 12 chunks at 80 ms

    cap = Capped(FakeStream(), [b"S" * CHUNK_BYTES] * 2)
    time.sleep(0.25)  # far longer than the cap needs
    pcm = cap.stop()
    assert len(pcm) == 12 * CHUNK_BYTES, f"cap leaked: {len(pcm) // CHUNK_BYTES} chunks"


def test_wake_chime_waits_for_the_end_of_speech():
    """A chime landing over the tail of an utterance is jarring: loud chunks
    hold it back, only a real gap releases it, and only once."""
    import numpy as np

    def chunk(level):
        return np.full(CHUNK_SAMPLES, level, np.int16).tobytes()

    fired = []

    def watcher():
        """_watch's state without a thread or a stream; the chime lands in
        `fired`."""
        cap = WakeCapture.__new__(WakeCapture)
        vars(cap).update(
            _t0=time.monotonic(),
            _quiet=0,
            _peak=0.0,
            _chime_deadline=True,
            _on_quiet=lambda: fired.append(time.monotonic()),
        )
        return cap

    cap = watcher()
    for _ in range(12):  # ~1 s of talking
        cap._watch(chunk(8000))
    time.sleep(0.05)  # the callback runs on its own thread
    assert not fired, "chimed while the user was still talking"

    for _ in range(6):  # ~480 ms gap: past QUIET_MS
        cap._watch(chunk(60))
    time.sleep(0.05)
    assert len(fired) == 1, f"want one chime at the gap, got {len(fired)}"
    for _ in range(10):
        cap._watch(chunk(60))
    time.sleep(0.05)
    assert len(fired) == 1, "chimed more than once"

    # Room too loud to hear a gap (TV up): chime anyway rather than never.
    late = watcher()
    late._t0 -= WakeCapture.CHIME_BY_S
    late._watch(chunk(8000))
    time.sleep(0.05)
    assert len(fired) == 2, "noisy room never got its chime"

    # After handoff, ongoing speech suppresses the deadline but silence still
    # triggers the chime.
    held = watcher()
    held._t0 -= WakeCapture.CHIME_BY_S
    held.disarm_deadline()
    for _ in range(12):  # still talking, past the deadline
        held._watch(chunk(8000))
    time.sleep(0.05)
    assert len(fired) == 2, "disarmed deadline still beeped over speech"
    for _ in range(6):  # the real gap still earns the chime
        held._watch(chunk(60))
    time.sleep(0.05)
    assert len(fired) == 3, "quiet detection must survive the disarm"


def test_wake_ack_is_claimed_exactly_once():
    """Only one thread can claim the wake acknowledgement."""
    ack = WakeAck()
    wins = []
    ready = threading.Barrier(8)

    def contend():
        ready.wait()
        wins.append(ack.claim())

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 1, f"{sum(wins)} claimants won the wake chime"
    assert not ack.claim(), "a later claim must still lose"
    # age() is what the gate folds a fast success against.
    assert 0 <= ack.age() < 1, f"age {ack.age()} after an immediate claim"
    assert WakeAck().age() == float("inf"), "unclaimed must not read as 'just chimed'"


async def test_feeder_stops_capture_at_startframe_and_feeds_it_in_hops(monkeypatch):
    """The feeder stops capture at StartFrame and preserves every sample."""
    from pipecat.frames.frames import InputAudioRawFrame, StartFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    feeder = PrerollFeeder(CapturingLog("preroll"))
    marker = b"\xbb" * CHUNK_BYTES
    capture = WakeCapture(FakeStream(), [marker, b"\xcc" * 100])
    monkeypatch.setattr(feeder, "capture", capture)
    pushed = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    async def base_noop(self, frame, direction):
        pass  # StartFrame bookkeeping needs a live pipeline

    monkeypatch.setattr(feeder, "push_frame", fake_push)
    monkeypatch.setattr(FrameProcessor, "process_frame", base_noop)
    await feeder.process_frame(
        InputAudioRawFrame(
            audio=b"\x00" * CHUNK_BYTES, sample_rate=SAMPLE_RATE, num_channels=1
        ),
        FrameDirection.DOWNSTREAM,
    )
    assert feeder.capture is capture and capture._pcm is None, (
        "the capture must keep pumping until StartFrame"
    )
    await feeder.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    assert feeder.capture is None and capture._pcm, "StartFrame must stop the capture"
    start = next(i for i, f in enumerate(pushed) if isinstance(f, StartFrame))
    fed = pushed[start + 1 :]
    assert b"".join(f.audio for f in fed) == capture._pcm, "pre-roll must be fed whole"
    assert fed[0].audio == marker and len(fed[-1].audio) == 100, fed
    assert all(len(f.audio) == CHUNK_BYTES for f in fed[:-1])
    assert all(f.sample_rate == 16000 and f.num_channels == 1 for f in fed)


async def test_pipeline_ordering(monkeypatch):
    """A live worker sends StartFrame, pre-roll, then live microphone audio."""
    # This test requires the default PortAudio devices.
    helpers.wants("audio")
    import pyaudio
    from pipecat.frames.frames import InputAudioRawFrame, StartFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )
    from pipecat.workers.runner import WorkerRunner

    events = []

    class Collector(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, StartFrame):
                events.append(("start", b""))
            elif isinstance(frame, InputAudioRawFrame):
                events.append(("audio", frame.audio))
            await self.push_frame(frame, direction)

    marker = b"".join(bytes([0xA0 + i]) * CHUNK_BYTES for i in range(4))
    feeder = PrerollFeeder(CapturingLog("preroll"))
    pa = pyaudio.PyAudio()
    wake_stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )
    monkeypatch.setattr(
        feeder,
        "capture",
        WakeCapture(
            wake_stream,
            [marker[i : i + CHUNK_BYTES] for i in range(0, len(marker), CHUNK_BYTES)],
        ),
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=16000,
        )
    )
    worker = PipelineWorker(
        Pipeline([transport.input(), feeder, Collector(), transport.output()]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
        enable_rtvi=False,
        idle_timeout_secs=20,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    try:
        await asyncio.sleep(1.5)  # pipeline up + live mic flowing
        await worker.cancel(reason="test done")
        await asyncio.wait_for(run_task, timeout=10)
    finally:
        pa.terminate()

    kinds = [k for k, _ in events]
    assert kinds and kinds[0] == "start", f"first event was {kinds[:3]}"
    audio = [a for k, a in events if k == "audio"]
    assert len(audio) > 4, "no mic frames followed the pre-roll seed"
    assert b"".join(audio[:4]) == marker, (
        "pre-roll must arrive complete and first, before any live mic frame"
    )
    assert audio[4][:1] != b"\xa0", "live frames must not repeat the pre-roll"
