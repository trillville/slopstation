"""Test the busy tone: one acknowledgment per turn for a slow tool call."""

import asyncio

import pytest
from pipecat.frames.frames import (
    EndFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from helpers import CapturingLog
from slopstation.agent.speech import earcons
from slopstation.agent.speech.busy import BusyTone

AFTER = 0.05


def started(call_id, name="find_tools"):
    return FunctionCallInProgressFrame(
        function_name=name,
        tool_call_id=call_id,
        arguments={},
        cancel_on_interruption=True,
    )


def finished(call_id, name="find_tools"):
    return FunctionCallResultFrame(
        function_name=name, tool_call_id=call_id, arguments={}, result={"ok": True}
    )


@pytest.fixture
def drive(monkeypatch):
    """Feed timed frames to a BusyTone with push_frame stubbed; returns what it
    pushed besides the frames it forwarded, and its log."""

    def _drive(script, phrase=""):
        log = CapturingLog("voice")
        busy = BusyTone(log, after_s=AFTER, phrase=phrase)
        pushed = []

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        monkeypatch.setattr(busy, "push_frame", fake_push)

        async def run():
            fed = []
            for item in script:
                if isinstance(item, float | int):
                    await asyncio.sleep(item)
                else:
                    fed.append(item)
                    await busy.process_frame(item, FrameDirection.DOWNSTREAM)
            await asyncio.sleep(AFTER * 3)  # let any armed timer land
            await busy.cleanup()
            return [f for f in pushed if f not in fed]

        return asyncio.run(run()), log

    return _drive


def test_a_slow_call_gets_one_earcon_and_a_fast_one_gets_nothing(drive):
    spoken, log = drive([started("a"), AFTER * 2, finished("a")])
    assert len(spoken) == 1 and isinstance(spoken[0], OutputAudioRawFrame)
    assert spoken[0].audio == earcons.pcm("busy")
    assert log.find("busy_tone")[-1]["tool"] == "find_tools"
    assert log.find("busy_tone")[-1]["kind"] == "earcon"
    # Back before the threshold: silence.
    spoken, log = drive([started("b"), AFTER / 4, finished("b")])
    assert spoken == [] and not log.find("busy_tone")


def test_one_turn_speaks_once_however_many_calls_and_a_new_turn_resets(drive):
    # find_tools, then the real tool, then a parallel pair: one tone.
    spoken, _ = drive(
        [
            started("a"),
            AFTER * 2,
            finished("a"),
            started("b", "list_torrents"),
            started("c", "disk_usage"),
            AFTER * 2,
            finished("b", "list_torrents"),
            finished("c", "disk_usage"),
        ]
    )
    assert len(spoken) == 1
    # The user speaks again: the next slow call may speak again.
    spoken, log = drive(
        [
            started("a"),
            AFTER * 2,
            finished("a"),
            UserStartedSpeakingFrame(),
            started("d", "media_history"),
            AFTER * 2,
            finished("d", "media_history"),
        ]
    )
    assert len(spoken) == 2 and [r["tool"] for r in log.find("busy_tone")] == [
        "find_tools",
        "media_history",
    ]


def test_a_new_turn_or_the_end_cancels_a_pending_timer(drive):
    spoken, _ = drive([started("a"), UserStartedSpeakingFrame(), AFTER * 2])
    assert spoken == [], "the turn moved on before the tone was due"
    spoken, _ = drive([started("a"), EndFrame(), AFTER * 2])
    assert spoken == [], "the session ended before the tone was due"


def test_a_phrase_is_spoken_instead_of_the_earcon(drive):
    spoken, log = drive([started("a"), AFTER * 2, finished("a")], phrase=" one moment ")
    assert len(spoken) == 1 and isinstance(spoken[0], TTSSpeakFrame)
    assert spoken[0].text == "one moment"
    assert log.find("busy_tone")[-1]["kind"] == "phrase"
