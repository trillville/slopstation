"""Wake pre-roll: keep mic audio flowing across the wake->pipeline gap.

The session transport reopens the mic ~0.5-2 s after detection, so without
this "hey jarvis volume up" loses "volume up". WakeCapture keeps reading the
wake stream; PrerollFeeder, a stage between transport.input() and Flux,
replays that PCM during StartFrame processing - frames move serially through
one queue, so Flux sees [StartFrame, pre-roll, live mic] in order, and Flux
holds queued audio until its websocket handshake is confirmed.

The transcript therefore starts with the wake phrase; grammar_gate.strip_wake
removes it text-side.
"""
import threading
import time

import numpy as np

from pipecat.frames.frames import Frame, InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

SAMPLE_RATE = 16000
BYTES_PER_S = SAMPLE_RATE * 2                   # mono s16
CHUNK_SAMPLES = 1280                            # the wake loop's 80 ms hop
CHUNK_BYTES = CHUNK_SAMPLES * 2
CHUNK_MS = CHUNK_SAMPLES * 1000 // SAMPLE_RATE  # 80


def _rms(chunk):
    x = np.frombuffer(chunk, np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


class WakeAck:
    """One wake chime per session, claimed by whichever of the capture watcher
    below and GrammarGate first sees the user stop talking - different
    threads, hence the lock. The timestamp lets the gate fold a success earcon
    landing on the chime into it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._at = None

    def claim(self):
        """True for exactly one caller - the winner plays the chime."""
        with self._lock:
            if self._at is not None:
                return False
            self._at = time.monotonic()
            return True

    def age(self):
        """Seconds since the chime was claimed; inf while unclaimed."""
        with self._lock:
            return float("inf") if self._at is None else time.monotonic() - self._at


class WakeCapture:
    """Owns the wake stream from detection until stop(). stop() is idempotent
    and returns all PCM captured (pre-detection ring included)."""

    MAX_S = 30              # runaway guard: stop growing if a session build stalls
    QUIET_MS = 350          # end-of-speech gap that earns the wake chime
    QUIET_RATIO = 0.18      # of the loudest speech heard since the wake word
    CHIME_BY_S = 1.5        # too noisy to tell -> chime anyway, never not at all

    def __init__(self, stream, seed_chunks, on_quiet=None):
        self._stream = stream
        self._chunks = list(seed_chunks)
        self._pcm = None
        self._on_quiet = on_quiet
        self._t0 = time.monotonic()
        self._quiet = 0
        # The wake phrase in the ring sets the speech scale.
        self._peak = max([_rms(c) for c in self._chunks] or [0.0]) if on_quiet else 0.0
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _watch(self, chunk):
        """Fire the wake chime at end of speech, not at detection. Levels are
        relative to the wake phrase, so a loud TV doesn't read as talking;
        too loud to call chimes at CHIME_BY_S. Playback on its own thread -
        stalling the pump would drop mic audio."""
        if self._on_quiet is None:
            return
        level = _rms(chunk)
        self._peak = max(self._peak, level)
        self._quiet = 0 if level >= self._peak * self.QUIET_RATIO else self._quiet + 1
        if (self._quiet * CHUNK_MS >= self.QUIET_MS
                or time.monotonic() - self._t0 >= self.CHIME_BY_S):
            fn, self._on_quiet = self._on_quiet, None
            threading.Thread(target=fn, daemon=True).start()

    def _pump(self):
        limit = self.MAX_S * BYTES_PER_S // CHUNK_BYTES
        while not self._stopping.is_set() and len(self._chunks) < limit:
            try:
                chunk = self._stream.read(
                    CHUNK_SAMPLES, exception_on_overflow=False)
            except OSError:
                break       # device vanished (BT flap) - keep what we have
            self._chunks.append(chunk)
            self._watch(chunk)

    def stop(self):
        if self._pcm is not None:
            return self._pcm
        self._stopping.set()
        self._thread.join(timeout=1.0)
        try:
            self._stream.stop_stream()
            self._stream.close()
        except OSError:
            pass
        self._pcm = b"".join(self._chunks)
        return self._pcm


class PrerollFeeder(FrameProcessor):
    """Replays wake-capture PCM ahead of live mic audio. pcm is assigned right
    before the runner starts, so capture covers most of the session build."""

    def __init__(self, log):
        super().__init__()
        self._log = log
        self.pcm = b""

    def _frames(self):
        return [InputAudioRawFrame(audio=self.pcm[i:i + CHUNK_BYTES],
                                   sample_rate=SAMPLE_RATE, num_channels=1)
                for i in range(0, len(self.pcm), CHUNK_BYTES)]

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame) and self.pcm:
            self._log("preroll_fed", audio_s=round(len(self.pcm) / BYTES_PER_S, 1))
            for f in self._frames():
                await self.push_frame(f)
            self.pcm = b""
