"""Judge the room against the talker who said the wake word.

Sits between the pre-roll feeder and the STT. The talker's level is the
reference; each transcript is stamped with level_db (its peak against the
reference) and quiet_ms (how long after the talker went quiet it arrived).

With chatterFloorDb set, the mic gate replaces the room with silence once the
talker has been quiet for QUIET_MS and the level is under the floor: Flux
closes the turn on the talker's silence, and chatter under the floor never
becomes a turn. A hop back near the floor reopens it.

The reference is the first LIVE turn, after the replay: the pre-roll is
recorded before the duck, when the TV reaches the mic 10-20 dB above a
talker. No reference, or a duck that did not land, means measure only.
Someone speaking well under the reference is muted with the room; 0 is off.
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
    """Per-turn level and quiet timing against the talker; the mic gate."""

    QUIET_MS = 350  # WakeCapture's bar for a gap
    QUIET_RATIO = 0.18  # the line with no floor set (~ -15 dB)
    REOPEN_RATIO = 0.5  # reopen 6 dB under the floor, so onsets pass
    LOG_MIN_MS = 1000  # shorter mutes are gaps between words

    def __init__(self, floor_db: float = 0.0, log=None, loud=None) -> None:
        super().__init__()
        self.floor_db = float(floor_db)  # 0 = measure only
        self.log = log
        self.loud = loud  # callable; True while the duck did not land
        self.reference = 0.0  # RMS of the talker; 0 = not yet known
        self.live = False  # past the replay
        self._peak = 0.0  # loudest hop since the last snapshot
        self._loud_at: float | None = None  # last hop over the line
        self._gated_since: float | None = None  # the gate closed at
        self._gated_peak = 0.0  # loudest hop while closed

    @property
    def gated(self) -> bool:
        return self._gated_since is not None

    def go_live(self) -> None:
        """The replay is over; the first turn from here sets the reference."""
        self.live = True
        self._peak = 0.0

    def _line(self) -> float:
        ratio = 10 ** (-self.floor_db / 20) if self.floor_db > 0 else self.QUIET_RATIO
        return (self.reference or self._peak) * ratio

    def _can_gate(self) -> bool:
        return (
            self.floor_db > 0
            and self.reference > 0
            and self.live
            and not (self.loud is not None and self.loud())
        )

    def hear(self, chunk: bytes, now: float | None = None) -> bytes:
        """One hop in; the hop, or silence while gated, out."""
        now = time.monotonic() if now is None else now
        level = _rms(chunk)
        self._peak = max(self._peak, level)
        line = self._line()
        if level > 0 and level >= line:
            self._loud_at = now
        since = self._gated_since
        gated = since is not None
        if since is not None and level >= line * self.REOPEN_RATIO:
            gated_ms = int(round((now - since) * 1000))
            if self.log is not None and gated_ms >= self.LOG_MIN_MS:
                self.log(
                    "mic_gated",
                    gated_ms=gated_ms,
                    peak_db=self._rel_db(self._gated_peak),
                )
            self._gated_since, self._gated_peak = None, 0.0
            return chunk
        if (
            not gated
            and level < line
            and self._can_gate()
            and self._loud_at is not None
            and (now - self._loud_at) * 1000 >= self.QUIET_MS
        ):
            self._gated_since, gated = now, True
        if gated:
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
        """The turn that just ended: level_db (peak vs the reference; None
        before one exists) and quiet_ms (None while the talker is still
        talking). Resets the peak."""
        now = time.monotonic() if now is None else now
        if not self.reference and self.live and self._peak:
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
