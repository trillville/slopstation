"""Wake pre-roll: keep mic audio flowing across the wake->pipeline gap.

"hey jarvis volume up" spoken as one sentence used to lose "volume up": the
wake stream closed at detection and the session transport reopens the mic
~0.5-2 s later, so command words landed in dead air. Two pieces close the gap:

  WakeCapture   - at detection the wake stream is NOT closed; a thread keeps
                  reading it (seeded with a rolling pre-detection ring, so the
                  wake phrase itself is included) until run_session stops it
                  right before the transport reopens the mic.
  PrerollFeeder - a pipeline stage between transport.input() and Flux that
                  replays the captured PCM during its StartFrame processing.
                  Frame ordering does the correctness work: each processor
                  handles frames serially through one input queue, so Flux
                  sees [StartFrame, pre-roll, live mic] strictly in order -
                  and Flux's own StartFrame handling (which awaits the
                  websocket handshake) holds all queued audio until the
                  socket is confirmed, so nothing is dropped.

The transcript now starts with the wake phrase; grammar_gate.strip_wake owns
removing it text-side (more reliable than trying to trim it out of the audio).

The capture window is also where the wake chime is timed from (WakeAck): it
is the only place that can hear you between the wake word and the pipeline.
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
    """One wake chime per session, claimed by whichever side first sees the
    user stop talking: the capture watcher below (mic still ours, chime played
    straight to PyAudio) or GrammarGate once the pipeline owns the mic (chime
    pushed as a frame). Those are different threads, hence the lock.

    It also remembers WHEN, which is what lets the gate fold a success earcon
    landing on top of the chime into it."""

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
        """Seconds since the chime was claimed; inf while unclaimed, so a
        caller asking "did it just chime?" gets a truthful no."""
        with self._lock:
            return float("inf") if self._at is None else time.monotonic() - self._at


class WakeCapture:
    """Owns the wake stream from detection until stop(). stop() is idempotent
    and returns all PCM captured (pre-detection ring + everything since)."""

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
        # The wake phrase is in the ring, so it sets the scale for "this is
        # what speech sounds like here" - no absolute threshold to calibrate.
        self._peak = max([_rms(c) for c in self._chunks] or [0.0]) if on_quiet else 0.0
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _watch(self, chunk):
        """Fire the wake chime once you STOP talking rather than the instant
        the wake word lands: a chime over the tail of "hey jarvis put on Elden
        Ring" is the jarring part, and landing it at end-of-speech also masks
        the wait before the answer. Levels are relative to the wake phrase
        itself, so a loud TV doesn't read as talking and a quiet room doesn't
        read as silence; if the room is too loud to call, chime at CHIME_BY_S
        anyway. Whoever loses this race (usually the pipeline, on a one-breath
        command that outlasts the session build) leaves the ack unclaimed for
        GrammarGate to play at end of turn.

        Playback goes on its own thread: stalling the pump for the length of a
        chime would drop mic audio - the words this module exists to keep."""
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
                break       # device vanished (BT flap) - keep what we already have
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
    """Replays wake-capture PCM ahead of live mic audio. pcm is assigned late
    (right before the runner starts) so the capture covers as much of the
    session build as possible."""

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
            self._log(f"pre-roll: feeding {len(self.pcm) / BYTES_PER_S:.1f}s "
                      f"of wake-window audio to STT")
            for f in self._frames():
                await self.push_frame(f)
            self.pcm = b""
