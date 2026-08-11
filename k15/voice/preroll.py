"""Wake pre-roll: keep mic audio flowing across the wake->pipeline gap.

"hey jarvis volume up" spoken as one sentence used to lose "volume up": the
wake stream closed at detection and the session transport reopens the mic
~0.5-2 s later, so command words landed in dead air (assumptions row 10).
Two pieces close the gap:

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
"""
import threading

from pipecat.frames.frames import Frame, InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

SAMPLE_RATE = 16000
BYTES_PER_S = SAMPLE_RATE * 2                   # mono s16
CHUNK_SAMPLES = 1280                            # the wake loop's 80 ms hop
CHUNK_BYTES = CHUNK_SAMPLES * 2


class WakeCapture:
    """Owns the wake stream from detection until stop(). stop() is idempotent
    and returns all PCM captured (pre-detection ring + everything since)."""

    MAX_S = 30              # runaway guard: stop growing if a session build stalls

    def __init__(self, stream, seed_chunks):
        self._stream = stream
        self._chunks = list(seed_chunks)
        self._pcm = None
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        limit = self.MAX_S * BYTES_PER_S // CHUNK_BYTES
        while not self._stopping.is_set() and len(self._chunks) < limit:
            try:
                self._chunks.append(self._stream.read(
                    CHUNK_SAMPLES, exception_on_overflow=False))
            except OSError:
                break       # device vanished (BT flap) - keep what we already have

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
