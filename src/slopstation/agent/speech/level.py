"""Measure the room against the talker who said the wake word.

Sits between the pre-roll feeder and the STT, so it hears exactly what the
recogniser hears: the wake phrase from the pre-roll, then the live mic. The
wake phrase sets the reference level; every transcript the gate handles is
then judged against it (level_db: how far under the wake phrase the loudest
moment of that turn was) and timed against the talker's own end of speech
(quiet_ms: how long after they went quiet the transcript arrived).

Measurement only, until the numbers say where the thresholds sit.
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
    """Per-turn level and end-of-speech timing relative to the wake phrase."""

    QUIET_MS = 350  # WakeCapture's bar: this much under the line is a gap
    QUIET_RATIO = 0.18  # of the reference; the fraction WakeCapture chimes at

    def __init__(self, reference: float = 0.0) -> None:
        super().__init__()
        # RMS of the wake phrase; 0 = unknown (a follow-up open has no
        # capture), in which case the first turn's peak becomes it.
        self.reference = float(reference)
        self._peak = 0.0  # loudest hop since the last snapshot
        self._loud_at: float | None = None  # last hop over the quiet line

    def hear(self, chunk: bytes, now: float | None = None) -> None:
        """One hop of mic audio."""
        now = time.monotonic() if now is None else now
        level = _rms(chunk)
        self._peak = max(self._peak, level)
        line = (self.reference or self._peak) * self.QUIET_RATIO
        if level >= line and level > 0:
            self._loud_at = now

    def snapshot(self, now: float | None = None) -> dict:
        """The turn that just ended: level_db (peak vs the reference, <= 0
        means quieter than the wake phrase) and quiet_ms (since the talker
        went quiet; None while they are still talking). Resets the peak."""
        now = time.monotonic() if now is None else now
        if not self.reference and self._peak:
            self.reference = self._peak
        peak, self._peak = self._peak, 0.0
        level_db = (
            round(20 * math.log10(peak / self.reference), 1)
            if peak > 0 and self.reference > 0
            else None
        )
        quiet_ms = None
        if self._loud_at is not None:
            gap = (now - self._loud_at) * 1000 - self.QUIET_MS
            quiet_ms = int(round(gap)) if gap >= 0 else None
        return {"level_db": level_db, "quiet_ms": quiet_ms}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.hear(frame.audio)
        await self.push_frame(frame, direction)
