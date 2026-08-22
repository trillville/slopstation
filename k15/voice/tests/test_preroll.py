"""Blind test: the wake pre-roll path.

Part 1 (pure): WakeCapture pump/stop semantics against a fake stream -
seed ring + pumped chunks in order, idempotent stop, stream closed, device
death mid-pump keeps what we have, runaway cap - plus the wake chime's
end-of-speech timing and the single-winner ack it is claimed through.

Part 2 (real devices, like test_session_pipeline): a running PipelineWorker
with [transport.input(), PrerollFeeder, collector] proves the ordering
contract the feature rests on: StartFrame first, then the ENTIRE pre-roll,
only then live mic audio - no interleave, no loss. Run:
    .venv\\Scripts\\python tests\\test_preroll.py
"""
import _bootstrap  # noqa: F401
import asyncio
import threading
import time
from pathlib import Path

import cglib
from preroll import CHUNK_BYTES, CHUNK_SAMPLES, PrerollFeeder, WakeAck, WakeCapture


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


def test_dead_wake_stream_surfaces_original_error():
    """A -9999 mid-listen makes cleanup raise 'Stream not open', which would
    replace the real error AND escape the handler. The listener must re-raise
    the ORIGINAL OSError."""
    import audio

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

    lst = audio.WakeListener.__new__(audio.WakeListener)
    lst.pa = FakePA()
    lst.device_index = None
    try:
        lst.wait_for_wake_capture(0.5)
        assert False, "dead stream must raise"
    except OSError as e:
        assert "Unanticipated" in str(e), f"original error was replaced: {e}"

    audio.close_stream_quietly(DeadStream())   # must not raise
    print("OK - dead wake stream: original error surfaces, quiet close swallows")


def test_zombie_stream_trips_silence_watchdog():
    """After a device flap the reopened stream can 'work' while delivering only
    zeros - no error, no wake. A solid run of zero chunks must raise into the
    same OSError recovery path as an honest stream death, and any real audio
    must reset the counter (a live mic always carries a noise floor)."""
    import numpy as np
    import audio

    NOISY_AT = 10                       # one real chunk mid-run resets the count

    class ZombieStream:
        def __init__(self):
            self.n = 0

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
        # **kw so this stub tracks openWakeWord's real predict(), which also
        # takes patience/threshold/debounce_time - score_chunk passes the
        # first two on every hop and a positional-only fake would fail here
        # for a reason that has nothing to do with the watchdog being drilled.
        def predict(self, chunk, **kw):
            return {"hey_jarvis": 0.0}

    lst = audio.WakeListener.__new__(audio.WakeListener)
    lst.np = np
    lst.model = FakeModel()
    stream = ZombieStream()
    try:
        lst._listen(stream, 0.5, None, None)
        assert False, "zombie stream must raise"
    except OSError as e:
        assert "zeros" in str(e), f"wrong error: {e}"
    want = NOISY_AT + audio.WakeListener.SILENT_CHUNKS
    assert stream.n == want, f"tripped after {stream.n} chunks, want {want}"
    print(f"OK - zombie stream: watchdog trips after {want} chunks, "
          f"real audio resets the count")


def test_near_miss_reports_one_event_per_run_with_its_peak():
    """Recall's only trace. A wake word that does not fire emits nothing, so
    every missed "hey alfred" on 2026-08-15 was invisible and the threshold
    argument could not be settled either way. One event per contiguous run
    above the floor, carrying that run's high-water mark - per-hop events
    would bury the one number worth reading."""
    import numpy as np

    import audio

    # floor = threshold 0.5 * factor 0.2 = 0.10. Two runs, then a crossing.
    scores = [0.02, 0.12, 0.28, 0.19, 0.03,     # run A, peak 0.28
              0.41, 0.07,                       # run B, peak 0.41
              0.55]                             # crosses

    class ScriptedModel:
        def __init__(self):
            self.i = 0
            self.reset_calls = 0

        def predict(self, chunk, **kw):
            self.i += 1
            return {"hey_jarvis": scores[self.i - 1]}

        def reset(self):
            self.reset_calls += 1

    class LiveStream:
        def read(self, n, exception_on_overflow=True):
            return b"\x01\x00" * n              # non-zero: watchdog stays quiet

    lst = audio.WakeListener.__new__(audio.WakeListener)
    lst.np = np
    lst.model = ScriptedModel()
    lst.near_miss_factor = 0.2

    real_log = audio.log
    audio.log = cglib.CapturingLog()
    try:
        score, peak = lst._listen(LiveStream(), 0.5, None, None)
        misses = audio.log.find("wake_near_miss")
    finally:
        audio.log = real_log

    assert (score, peak) == (0.55, 0.55), (score, peak)
    assert len(misses) == 2, f"want one event per run, got {len(misses)}"
    assert [m["peak"] for m in misses] == [0.28, 0.41], misses
    assert misses[0]["shortfall"] == 0.22, misses[0]
    assert lst.model.reset_calls == 1, lst.model.reset_calls
    print("  OK  near miss: one event per run, carrying that run's peak")


def test_clip_dump_writes_prunes_and_never_raises():
    """The false-activation corpus openWakeWord's custom verifier trains its
    negatives on. Capped because this writes on every single fire, and
    fail-soft because a session is already being built behind the call - a
    full disk owes us a log line, not a dead wake path."""
    import tempfile
    import wave

    import audio

    ring = [b"\x01\x00" * CHUNK_SAMPLES] * 3
    tmp = Path(tempfile.mkdtemp())
    real_dir, real_log = audio.CLIPS_DIR, audio.log
    audio.CLIPS_DIR = tmp / "wake"
    audio.log = cglib.CapturingLog()
    try:
        for i in range(5):
            audio.dump_clip(ring, 0.20 + i / 100, keep=3)
        kept = sorted(audio.CLIPS_DIR.glob("wake-*.wav"))
        written = audio.log.find("wake_clip")

        # keep=0 is the off switch, and it must not even make the directory.
        audio.CLIPS_DIR = tmp / "off"
        audio.dump_clip(ring, 0.5, keep=0)
        off_made = (tmp / "off").exists()

        # Fail-soft: a CLIPS_DIR that cannot be created (here: under a FILE)
        # must log and return, never raise into the wake path.
        blocker = tmp / "blocker"
        blocker.write_bytes(b"")
        audio.CLIPS_DIR = blocker / "wake"
        audio.dump_clip(ring, 0.5, keep=3)
        failures = audio.log.find("wake_clip_failed")
    finally:
        audio.CLIPS_DIR, audio.log = real_dir, real_log

    assert len(written) == 5, f"5 fires must log 5 clips, got {len(written)}"
    assert len(kept) == 3, f"keep=3 must prune to 3, got {len(kept)}"
    # Pruning keeps the NEWEST, and names sort chronologically - so the three
    # survivors are the last three scores written.
    assert [p.name.split("-")[-1] for p in kept] == ["0.220.wav", "0.230.wav",
                                                     "0.240.wav"], kept
    with wave.open(str(kept[0]), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert w.getnframes() == 3 * CHUNK_SAMPLES, w.getnframes()
    assert not off_made, "keep=0 must not create the directory"
    assert len(failures) == 1 and failures[0]["level"] == "warn", failures
    print("  OK  wake clips: written, pruned to the cap, fail-soft on a bad dir")


def test_wake_chime_waits_for_the_end_of_speech():
    """The point of the whole watcher: a chime landing over "hey jarvis put on
    Elden Ring" is jarring, so loud chunks must hold it back and only a real
    gap may release it - once."""
    import numpy as np

    def chunk(level):
        return np.full(CHUNK_SAMPLES, level, np.int16).tobytes()

    def watcher():
        """_watch's state without a thread or a stream - the logic under test."""
        cap = WakeCapture.__new__(WakeCapture)
        cap._t0, cap._quiet, cap._peak = time.monotonic(), 0, 0.0
        return cap

    fired = []
    cap = watcher()
    cap._on_quiet = lambda: fired.append(time.monotonic())
    for _ in range(12):                 # ~1 s of talking
        cap._watch(chunk(8000))
    time.sleep(0.05)                    # the callback runs on its own thread
    assert not fired, "chimed while the user was still talking"

    for _ in range(6):                  # ~480 ms gap: past QUIET_MS
        cap._watch(chunk(60))
    time.sleep(0.05)
    assert len(fired) == 1, f"want one chime at the gap, got {len(fired)}"
    for _ in range(10):
        cap._watch(chunk(60))
    time.sleep(0.05)
    assert len(fired) == 1, "chimed more than once"

    # Room too loud to hear a gap (TV up): chime anyway rather than never.
    late = watcher()
    late._on_quiet = lambda: fired.append(time.monotonic())
    late._t0 -= WakeCapture.CHIME_BY_S
    late._watch(chunk(8000))
    time.sleep(0.05)
    assert len(fired) == 2, "noisy room never got its chime"
    print("OK - wake chime: held through speech, fired once at the gap, "
          "and forced after CHIME_BY_S when the room is too loud to tell")


def test_wake_ack_is_claimed_exactly_once():
    """Capture watcher and GrammarGate race for it from different threads;
    two winners means two chimes."""
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
    # age() is what the gate folds a fast success earcon against.
    assert 0 <= ack.age() < 1, f"age {ack.age()} after an immediate claim"
    assert WakeAck().age() == float("inf"), "unclaimed must not read as 'just chimed'"
    print("OK - wake ack: exactly one of 8 racing claimants wins, age tracks it")


def test_feeder_chunking():
    feeder = PrerollFeeder(cglib.CapturingLog("preroll"))
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
    feeder = PrerollFeeder(cglib.CapturingLog("preroll"))
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
    test_dead_wake_stream_surfaces_original_error()
    test_zombie_stream_trips_silence_watchdog()
    test_near_miss_reports_one_event_per_run_with_its_peak()
    test_clip_dump_writes_prunes_and_never_raises()
    test_wake_chime_waits_for_the_end_of_speech()
    test_wake_ack_is_claimed_exactly_once()
    test_feeder_chunking()
    if _bootstrap.wants("mic"):                   # real devices; run.py --with mic
        asyncio.run(test_pipeline_ordering())
        print("OK - pre-roll: capture, chunking, and pipeline ordering all hold")
    else:
        print("OK - pre-roll: capture and chunking hold (pipeline ordering on the "
              "real mic skipped: run.py --with mic)")


if __name__ == "__main__":
    main()
