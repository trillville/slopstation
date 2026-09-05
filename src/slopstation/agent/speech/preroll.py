"""Preserve microphone audio recorded while a voice session starts."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

import numpy as np
from pipecat.frames.frames import Frame, InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

SAMPLE_RATE = 16000
BYTES_PER_S = SAMPLE_RATE * 2  # mono s16
CHUNK_SAMPLES = 1280  # the wake loop's 80 ms hop
CHUNK_BYTES = CHUNK_SAMPLES * 2
CHUNK_MS = CHUNK_SAMPLES * 1000 // SAMPLE_RATE  # 80


def _rms(chunk: bytes) -> float:
    x = np.frombuffer(chunk, np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


class WakeAck:
    """Let one caller claim the wake chime and record when it happened."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at: float | None = None

    def claim(self) -> bool:
        """Return true for the first caller only."""
        with self._lock:
            if self._at is not None:
                return False
            self._at = time.monotonic()
            return True

    def age(self) -> float:
        """Return seconds since the claim, or infinity before a claim."""
        with self._lock:
            return float("inf") if self._at is None else time.monotonic() - self._at


class WakeCapture:
    """Capture microphone audio until ``stop()`` returns the collected PCM."""

    MAX_S = 30  # Stop buffering if session setup stalls.
    QUIET_MS = 350  # Silence needed before the wake chime.
    QUIET_RATIO = 0.18  # Fraction of the loudest speech since the wake word.
    CHIME_BY_S = 1.5  # Chime by this deadline when silence is unclear.

    def __init__(self, stream, seed_chunks: Iterable[bytes], on_quiet=None) -> None:
        self._stream = stream
        self._chunks = list(seed_chunks)
        self._pcm: bytes | None = None
        self._on_quiet = on_quiet
        self._t0 = time.monotonic()
        self._quiet = 0
        self._chime_deadline = True  # CHIME_BY_S armed; see disarm_deadline
        # The wake phrase in the ring sets the speech scale.
        self._peak = max([_rms(c) for c in self._chunks] or [0.0]) if on_quiet else 0.0
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _watch(self, chunk: bytes) -> None:
        """Play the wake chime after speech ends or the deadline passes."""
        if self._on_quiet is None:
            return
        level = _rms(chunk)
        self._peak = max(self._peak, level)
        self._quiet = 0 if level >= self._peak * self.QUIET_RATIO else self._quiet + 1
        if self._quiet * CHUNK_MS >= self.QUIET_MS or (
            self._chime_deadline and time.monotonic() - self._t0 >= self.CHIME_BY_S
        ):
            fn, self._on_quiet = self._on_quiet, None
            threading.Thread(target=fn, daemon=True).start()

    @property
    def peak(self) -> float:
        """Loudest hop so far (RMS): the talker's level, wake phrase included."""
        return self._peak

    def disarm_deadline(self) -> None:
        """Disable the chime deadline after handing audio to the session."""
        self._chime_deadline = False

    def _pump(self) -> None:
        limit = self.MAX_S * BYTES_PER_S // CHUNK_BYTES
        while not self._stopping.is_set() and len(self._chunks) < limit:
            try:
                chunk = self._stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
            except OSError:
                break
            self._chunks.append(chunk)
            self._watch(chunk)

    def stop(self) -> bytes:
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


def _frames(pcm: bytes) -> list:
    """The PCM as input frames on the wake loop's 80 ms hop."""
    return [
        InputAudioRawFrame(
            audio=pcm[i : i + CHUNK_BYTES], sample_rate=SAMPLE_RATE, num_channels=1
        )
        for i in range(0, len(pcm), CHUNK_BYTES)
    ]


class PrerollFeeder(FrameProcessor):
    """Replays wake-capture PCM ahead of live mic audio. The capture is handed
    over LIVE and stopped here on StartFrame (see the module docstring); the
    wake stream and the transport's mic stream overlap for the tail of the
    build, so the last few chunks are captured twice - bounded by one
    StartFrame hop, and silence in the normal cadence."""

    def __init__(self, log) -> None:
        super().__init__()
        self._log = log
        self.capture = None  # WakeCapture, stopped on StartFrame
        # Called once the replay is fed; live audio follows.
        self.on_replayed: Callable[[], None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame) and self.capture is not None:
            pcm = self.capture.stop()
            self.capture = None
            if pcm:
                self._log("preroll_fed", audio_s=round(len(pcm) / BYTES_PER_S, 1))
                for f in _frames(pcm):
                    await self.push_frame(f)
        if isinstance(frame, StartFrame) and self.on_replayed is not None:
            self.on_replayed()
