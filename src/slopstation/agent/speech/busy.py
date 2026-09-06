"""Break a long silence while a tool runs.

The model goes quiet while a tool call is out: a slow read, or the extra
model turn after find_tools loads something. This processor sits between the
model and the speech synthesiser and watches the function-call frames the
model service emits. A call that is still out after `after_s` gets one
acknowledgment, the busy earcon or a spoken phrase, pushed down the same
queue the answer will use, so it plays in order and stops on talk-over like
any other speech. A call that returns first gets nothing, and a turn gets at
most one, however many calls the model makes in it. Tool code never knows.
"""

from __future__ import annotations

import asyncio

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from slopstation.agent.speech import earcons

AFTER_S = 0.8  # config voice.busyAfterMs; 0 turns the processor off
EARCON = "busy"


class BusyTone(FrameProcessor):
    """One acknowledgment per turn for a tool call that has kept the user
    waiting. `phrase` empty plays the busy earcon; otherwise the phrase is
    spoken."""

    def __init__(self, log, after_s: float = AFTER_S, phrase: str = "") -> None:
        super().__init__()
        self.log = log
        self.after_s = float(after_s)
        self.phrase = phrase.strip()
        self._timers: dict[str, asyncio.Task] = {}
        self._spoke = False  # this turn

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, FunctionCallInProgressFrame):
            self._arm(frame)
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self._disarm(frame.tool_call_id)
        elif isinstance(frame, (UserStartedSpeakingFrame, InterruptionFrame)):
            # A new turn, or the user talking over: whatever was pending is
            # moot, and the next slow call may speak again.
            self._disarm_all()
            self._spoke = False
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._disarm_all()
        await self.push_frame(frame, direction)

    async def cleanup(self):
        self._disarm_all()
        await super().cleanup()

    def _arm(self, frame: FunctionCallInProgressFrame) -> None:
        if self._spoke or frame.tool_call_id in self._timers:
            return
        self._timers[frame.tool_call_id] = asyncio.create_task(
            self._fire(frame.tool_call_id, frame.function_name)
        )

    def _disarm(self, tool_call_id: str) -> None:
        task = self._timers.pop(tool_call_id, None)
        if task is not None:
            task.cancel()

    def _disarm_all(self) -> None:
        for tool_call_id in list(self._timers):
            self._disarm(tool_call_id)

    async def _fire(self, tool_call_id: str, tool: str) -> None:
        await asyncio.sleep(self.after_s)
        self._timers.pop(tool_call_id, None)
        if self._spoke:
            return
        self._spoke = True
        self.log(
            "busy_tone",
            tool=tool,
            after_ms=int(self.after_s * 1000),
            kind="phrase" if self.phrase else "earcon",
        )
        if self.phrase:
            await self.push_frame(TTSSpeakFrame(self.phrase))
        else:
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=earcons.pcm(EARCON),
                    sample_rate=earcons.SAMPLE_RATE,
                    num_channels=1,
                )
            )
