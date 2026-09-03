"""The wake pre-roll path. Part 1 (pure): WakeCapture pump/stop
against a fake stream, the wake chime's end-of-speech timing, the single-winner
ack. Part 2 (real devices): a live PipelineWorker proves StartFrame first, then
the ENTIRE pre-roll, only then live mic audio.
"""

import asyncio
import dataclasses
import threading
import time

import pytest

import helpers
from slopstation import logbook
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


def bare_listener(**attrs):
    """A WakeListener without __init__ - no PortAudio, no model download; the
    class defaults cover the tuning knobs. `attrs` is what the test wires in."""
    from slopstation.agent.speech import audio

    lst = audio.WakeListener.__new__(audio.WakeListener)
    vars(lst).update(attrs)
    return lst


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


def test_dead_wake_stream_surfaces_original_error():
    """A -9999 mid-listen makes cleanup raise 'Stream not open', replacing the
    real error and escaping the handler. The original OSError must survive."""
    from slopstation.agent.speech import audio

    class DeadStream:
        def read(self, n, exception_on_overflow=True):
            raise OSError(-9999, "Unanticipated host error")

        def stop_stream(self):
            raise OSError("Stream not open")

        def close(self):
            raise OSError("Stream not open")

    class FakePA:
        def open(self, **kw):
            return DeadStream()

    lst = bare_listener(pa=FakePA(), device_index=None)
    with pytest.raises(OSError) as e:
        lst.wait_for_wake_capture(0.5)
    assert "Unanticipated" in str(e.value), f"original error was replaced: {e.value}"

    audio.close_stream_quietly(DeadStream())  # must not raise


def test_zombie_stream_trips_silence_watchdog():
    """After a device flap the reopened stream can deliver only zeros - no
    error, no wake. A solid run of zeros must raise into the OSError recovery
    path; real audio resets the count (a live mic has a noise floor)."""
    import numpy as np

    from slopstation.agent.speech import audio

    NOISY_AT = 10  # one real chunk mid-run resets the count

    class ZombieStream:
        n = 0

        def read(self, n, exception_on_overflow=True):
            self.n += 1
            if self.n == NOISY_AT:
                return b"\x01\x00" * n
            return b"\x00" * (n * 2)

        def stop_stream(self):
            pass

        def close(self):
            pass

    class FakeModel:
        # **kw tracks openWakeWord's real predict(), which also takes
        # patience/threshold/debounce_time - score_chunk passes the first two.
        def predict(self, chunk, **kw):
            return {"hey_jarvis": 0.0}

    lst = bare_listener(np=np, model=FakeModel())
    stream = ZombieStream()
    with pytest.raises(OSError, match="zeros"):
        lst._listen(stream, 0.5, None, None)
    want = NOISY_AT + audio.WakeListener.SILENT_CHUNKS
    assert stream.n == want, f"tripped after {stream.n} chunks, want {want}"


def test_near_miss_reports_one_event_per_run_with_its_peak(monkeypatch):
    """Recall's only trace: a wake word that does not fire emits nothing.
    One event per contiguous run above the floor, carrying that run's peak."""
    import numpy as np

    from slopstation.agent.speech import audio

    # floor = threshold 0.5 * factor 0.2 = 0.10. Two runs, then a crossing.
    scores = [
        0.02,
        0.12,
        0.28,
        0.19,
        0.03,  # run A, peak 0.28
        0.41,
        0.07,  # run B, peak 0.41
        0.55,
    ]  # crosses

    class ScriptedModel:
        i = 0
        reset_calls = 0

        def predict(self, chunk, **kw):
            self.i += 1
            return {"hey_jarvis": scores[self.i - 1]}

        def reset(self):
            self.reset_calls += 1

    class LiveStream:
        def read(self, n, exception_on_overflow=True):
            return b"\x01\x00" * n  # non-zero: watchdog stays quiet

    lst = bare_listener(np=np, model=ScriptedModel(), near_miss_factor=0.2)
    log = logbook.CapturingLog()
    monkeypatch.setattr(audio, "log", log)
    score, peak = lst._listen(LiveStream(), 0.5, None, None)
    misses = log.find("wake_near_miss")

    assert (score, peak) == (0.55, 0.55), (score, peak)
    assert len(misses) == 2, f"want one event per run, got {len(misses)}"
    assert [m["peak"] for m in misses] == [0.28, 0.41], misses
    assert misses[0]["shortfall"] == 0.22, misses[0]
    assert lst.model.reset_calls == 1, lst.model.reset_calls


def test_clip_dump_writes_prunes_and_never_raises(monkeypatch, tmp_path):
    """The false-activation corpus openWakeWord's verifier trains negatives
    on. Capped (writes on every fire) and fail-soft (a session is building)."""
    import wave

    from slopstation.agent.speech import audio

    ring = [b"\x01\x00" * CHUNK_SAMPLES] * 3
    log = logbook.CapturingLog()
    monkeypatch.setattr(audio, "log", log)
    # clips_dir() lives under this test's own logs/, so nothing to re-point.
    for i in range(5):
        audio.dump_clip(ring, 0.20 + i / 100, keep=3)
    kept = sorted(audio.clips_dir().glob("wake-*.wav"))
    written = log.find("wake_clip")

    # keep=0 is the off switch: not even the directory.
    monkeypatch.setattr(audio, "clips_dir", lambda: tmp_path / "off")
    audio.dump_clip(ring, 0.5, keep=0)
    off_made = (tmp_path / "off").exists()

    # Fail-soft: a CLIPS_DIR that cannot be created (here, under a file)
    # logs and returns, never raises into the wake path.
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    monkeypatch.setattr(audio, "clips_dir", lambda: blocker / "wake")
    audio.dump_clip(ring, 0.5, keep=3)
    failures = log.find("wake_clip_failed")

    assert len(written) == 5, f"5 fires must log 5 clips, got {len(written)}"
    assert len(kept) == 3, f"keep=3 must prune to 3, got {len(kept)}"
    # Pruning keeps the newest, and names sort chronologically.
    assert [p.name.split("-")[-1] for p in kept] == [
        "0.220.wav",
        "0.230.wav",
        "0.240.wav",
    ], kept
    with wave.open(str(kept[0]), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert w.getnframes() == 3 * CHUNK_SAMPLES, w.getnframes()
    assert not off_made, "keep=0 must not create the directory"
    assert len(failures) == 1 and failures[0]["level"] == "warn", failures


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

    # Disarmed at the pipeline handoff, the deadline must NOT beep over a
    # one-breath command still being spoken (the pump outlives the old stop
    # point by the whole setup) - but a real gap still chimes.
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
    """Capture watcher and GrammarGate race from different threads; two
    winners means two chimes."""
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


def test_feeder_chunking(monkeypatch):
    feeder = PrerollFeeder(logbook.CapturingLog("preroll"))
    monkeypatch.setattr(feeder, "pcm", b"\xaa" * (CHUNK_BYTES * 2 + 100))
    frames = feeder._frames()
    assert [len(f.audio) for f in frames] == [CHUNK_BYTES, CHUNK_BYTES, 100]
    assert all(f.sample_rate == 16000 and f.num_channels == 1 for f in frames)
    assert b"".join(f.audio for f in frames) == feeder.pcm
    monkeypatch.setattr(feeder, "pcm", b"")
    assert feeder._frames() == []


async def test_feeder_stops_capture_at_startframe(monkeypatch):
    """The capture must survive until StartFrame: pipecat 1.8 runs the Flux
    connect during setup, and words spoken there exist only if the wake
    stream is still pumping when setup runs."""
    from pipecat.frames.frames import InputAudioRawFrame, StartFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    feeder = PrerollFeeder(logbook.CapturingLog("preroll"))
    marker = b"\xbb" * CHUNK_BYTES
    capture = WakeCapture(FakeStream(), [marker])
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
    assert capture._pcm is not None, "StartFrame must stop the capture"
    fed = [
        f
        for f in pushed
        if isinstance(f, InputAudioRawFrame) and f.audio[:1] == b"\xbb"
    ]
    assert len(fed) == 1, "the captured PCM must be fed as pre-roll"
    assert feeder.capture is None and feeder.pcm == b""


async def test_pipeline_ordering(monkeypatch):
    """A live worker delivers StartFrame, then the whole pre-roll, then live
    mic frames - strictly in that order. The pre-roll comes from a REAL
    WakeCapture handed over live, so this also drills the 1.8 overlap: the
    wake stream stays open while the transport opens its own stream on the
    same mic during setup."""
    # LocalAudioTransport opens PortAudio's default devices for real - the
    # one test here a deviceless machine (CI) cannot run.
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
    feeder = PrerollFeeder(logbook.CapturingLog("preroll"))
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
