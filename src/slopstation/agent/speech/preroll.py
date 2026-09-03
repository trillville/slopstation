"""Wake pre-roll: keep mic audio flowing across the wake->pipeline gap.

The session transport delivers mic audio only from StartFrame, and pipecat
1.8 runs the Flux websocket connect during pipeline SETUP, before StartFrame
- so without this "hey jarvis volume up" loses "volume up". WakeCapture keeps
reading the wake stream through the whole session build; PrerollFeeder, a
stage between transport.input() and Flux, stops it on StartFrame and replays
the PCM - frames move serially through one queue, so Flux sees [StartFrame,
pre-roll, live mic] in order. The capture must run until StartFrame, not be
stopped at build start: the setup window (Flux handshake, 0.3-1.5 s) is
otherwise covered by neither the capture nor the live mic.

The transcript therefore starts with the wake phrase; grammar_gate.strip_wake
removes it text-side.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

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
    """One wake chime per session, claimed by whichever of the capture watcher
    below and GrammarGate first sees the user stop talking - different
    threads, hence the lock. The timestamp lets the gate fold a success earcon
    landing on the chime into it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at: float | None = None

    def claim(self) -> bool:
        """True for exactly one caller - the winner plays the chime."""
        with self._lock:
            if self._at is not None:
                return False
            self._at = time.monotonic()
            return True

    def age(self) -> float:
        """Seconds since the chime was claimed; inf while unclaimed."""
        with self._lock:
            return float("inf") if self._at is None else time.monotonic() - self._at


class WakeCapture:
    """Owns the wake stream from detection until stop(). stop() is idempotent
    and returns all PCM captured (pre-detection ring included)."""

    MAX_S = 30  # runaway guard: stop growing if a session build stalls
    QUIET_MS = 350  # end-of-speech gap that earns the wake chime
    QUIET_RATIO = 0.18  # of the loudest speech heard since the wake word
    CHIME_BY_S = 1.5  # too noisy to tell -> chime anyway, never not at all

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
        """Fire the wake chime at end of speech, not at detection. Levels are
        relative to the wake phrase, so a loud TV doesn't read as talking;
        too loud to call chimes at CHIME_BY_S. Playback on its own thread -
        stalling the pump would drop mic audio."""
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

    def disarm_deadline(self) -> None:
        """Retire the CHIME_BY_S fallback; quiet detection stays armed. Called
        at the pipeline handoff: the pump now outlives the old stop point by
        the whole setup (the Flux connect), where a one-breath command is
        still mid-word at 1.5 s - the deadline would beep over it. Past the
        handoff, quiet detection and the gate's final-transcript backstop own
        the chime."""
        self._chime_deadline = False

    def _pump(self) -> None:
        limit = self.MAX_S * BYTES_PER_S // CHUNK_BYTES
        while not self._stopping.is_set() and len(self._chunks) < limit:
            try:
                chunk = self._stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
            except OSError:
                break  # device vanished (BT flap) - keep what we have
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
        self.pcm = b""  # or pre-stopped PCM (tests)

    def _frames(self) -> list:
        return [
            InputAudioRawFrame(
                audio=self.pcm[i : i + CHUNK_BYTES],
                sample_rate=SAMPLE_RATE,
                num_channels=1,
            )
            for i in range(0, len(self.pcm), CHUNK_BYTES)
        ]

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            if self.capture is not None:
                self.pcm = self.capture.stop()
                self.capture = None
            if self.pcm:
                self._log("preroll_fed", audio_s=round(len(self.pcm) / BYTES_PER_S, 1))
                for f in self._frames():
                    await self.push_frame(f)
                self.pcm = b""
