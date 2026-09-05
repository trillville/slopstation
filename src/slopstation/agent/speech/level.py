"""Judge the room against the talker who said the wake word.

Sits between the pre-roll feeder and the STT, so it hears exactly what the
recogniser hears: the wake phrase from the pre-roll, then the live mic. The
wake phrase sets the reference level. Two things follow from it:

- Measurement. Every transcript the gate handles is stamped with level_db
  (how far under the wake phrase the loudest moment of that turn was) and
  quiet_ms (how long after the talker went quiet the transcript arrived).
- The mic gate. Once the talker has been quiet for QUIET_MS, whatever is
  still coming in under the floor (the ducked TV, someone across the room,
  the assistant's own voice back off the soundbar) is replaced with silence
  before it reaches the STT. Flux then hears the talker stop and closes the
  turn on its own, rather than transcribing the room until eot_timeout_ms;
  and chatter under the floor never becomes a turn at all. The first hop
  over the floor reopens the gate at once, so the talker is never cut.

The gate is the one thing here that can go wrong for a real person: someone
on the couch speaking well under the wake phrase's level is muted with it.
chatterFloorDb sets the floor; 0 turns the gate off and keeps the numbers.
"""

from __future__ import annotations

import math
import time

from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from slopstation.agent.speech.preroll import _rms

FULL_SCALE = 32768.0


def dbfs(level: float) -> float | None:
    """RMS in dB below full scale, or None for silence."""
    return round(20 * math.log10(level / FULL_SCALE), 1) if level > 0 else None


class RoomLevel(FrameProcessor):
    """Per-turn level and end-of-speech timing relative to the wake phrase,
    and the mic gate that silences the room under the floor."""

    QUIET_MS = 350  # WakeCapture's bar: this much under the line is a gap
    QUIET_RATIO = 0.18  # the measurement line with no floor set (~ -15 dB)

    def __init__(self, reference: float = 0.0, floor_db: float = 0.0, log=None) -> None:
        super().__init__()
        # RMS of the wake phrase; 0 = unknown (a follow-up open has no
        # capture), in which case the first turn's peak becomes it.
        self.reference = float(reference)
        # The room is this far under the talker. 0 = measure only, never mute.
        self.floor_db = float(floor_db)
        self.log = log
        self._peak = 0.0  # loudest hop since the last snapshot
        self._loud_at: float | None = None  # last hop over the line
        self._gated_since: float | None = None  # the gate closed at
        self._gated_peak = 0.0  # loudest hop while closed

    @property
    def gated(self) -> bool:
        return self._gated_since is not None

    def _line(self) -> float:
        ratio = 10 ** (-self.floor_db / 20) if self.floor_db > 0 else self.QUIET_RATIO
        return (self.reference or self._peak) * ratio

    def hear(self, chunk: bytes, now: float | None = None) -> bytes:
        """One hop of mic audio. Returns what the STT should hear: the hop,
        or silence while the gate is closed."""
        now = time.monotonic() if now is None else now
        level = _rms(chunk)
        self._peak = max(self._peak, level)
        talker = level > 0 and level >= self._line()
        if talker:
            self._loud_at = now
            if self._gated_since is not None:
                if self.log is not None:
                    self.log(
                        "mic_gated",
                        gated_ms=int(round((now - self._gated_since) * 1000)),
                        peak_db=self._rel_db(self._gated_peak),
                    )
                self._gated_since, self._gated_peak = None, 0.0
            return chunk
        if (
            self.floor_db > 0
            and self.reference > 0
            and self._loud_at is not None
            and (now - self._loud_at) * 1000 >= self.QUIET_MS
        ):
            if self._gated_since is None:
                self._gated_since = now
            self._gated_peak = max(self._gated_peak, level)
            return bytes(len(chunk))
        return chunk

    def _rel_db(self, level: float) -> float | None:
        return (
            round(20 * math.log10(level / self.reference), 1)
            if level > 0 and self.reference > 0
            else None
        )

    def snapshot(self, now: float | None = None) -> dict:
        """The turn that just ended: level_db (peak vs the reference, <= 0
        means quieter than the wake phrase) and quiet_ms (since the talker
        went quiet; None while they are still talking). Resets the peak."""
        now = time.monotonic() if now is None else now
        if not self.reference and self._peak:
            self.reference = self._peak
        peak, self._peak = self._peak, 0.0
        quiet_ms = None
        if self._loud_at is not None:
            gap = (now - self._loud_at) * 1000 - self.QUIET_MS
            quiet_ms = int(round(gap)) if gap >= 0 else None
        return {"level_db": self._rel_db(peak), "quiet_ms": quiet_ms}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            frame.audio = self.hear(frame.audio)
        await self.push_frame(frame, direction)
