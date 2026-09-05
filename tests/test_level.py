"""Test the room-level measurement behind level_db and quiet_ms."""

import asyncio

import numpy as np
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from helpers import CapturingLog
from slopstation.agent.speech.grammar_gate import GrammarGate, GrammarMatcher
from slopstation.agent.speech.level import RoomLevel, dbfs
from slopstation.agent.speech.preroll import CHUNK_SAMPLES


def chunk(level):
    return np.full(CHUNK_SAMPLES, level, np.int16).tobytes()


def test_level_is_measured_against_the_wake_phrase():
    lvl = RoomLevel(reference=8000)
    for _ in range(5):
        lvl.hear(chunk(8000), now=1.0)  # the talker, as loud as the wake word
    assert lvl.snapshot(now=1.0)["level_db"] == 0.0
    for _ in range(5):
        lvl.hear(chunk(800), now=2.0)  # chatter 20 dB down
    assert lvl.snapshot(now=2.0)["level_db"] == -20.0
    assert lvl.snapshot(now=3.0)["level_db"] is None, "an empty turn has no peak"


def test_a_follow_up_open_takes_the_first_turn_as_its_reference():
    lvl = RoomLevel()  # no capture, no wake phrase
    lvl.hear(chunk(4000), now=1.0)
    assert lvl.snapshot(now=1.0)["level_db"] == 0.0
    assert lvl.reference == 4000
    lvl.hear(chunk(2000), now=2.0)
    assert lvl.snapshot(now=2.0)["level_db"] == -6.0


def test_quiet_ms_counts_from_the_talkers_last_loud_hop():
    lvl = RoomLevel(reference=8000)
    lvl.hear(chunk(8000), now=10.0)
    lvl.hear(chunk(60), now=10.08)  # under the line: the gap begins at 10.0
    # Still inside the 350 ms gap: not quiet yet.
    assert lvl.snapshot(now=10.2)["quiet_ms"] is None
    # The final transcript arrives 1.35 s after the last loud hop: Flux took
    # a second past the talker's own end of speech.
    assert lvl.snapshot(now=11.35)["quiet_ms"] == 1000
    # Chatter under the line does not restart the clock.
    lvl.hear(chunk(600), now=11.5)
    assert lvl.snapshot(now=11.5)["quiet_ms"] == 1150


def test_dbfs():
    assert dbfs(32768) == 0.0 and dbfs(3276.8) == -20.0 and dbfs(0) is None


def test_the_gate_stamps_every_transcript_with_the_room(monkeypatch):
    lvl = RoomLevel(reference=8000)
    glog = CapturingLog("voice")

    class FakeDispatch:
        def begin_utterance(self, turn, text):
            pass

    gate = GrammarGate(GrammarMatcher({"inputs": {}}), FakeDispatch(), glog, level=lvl)

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    monkeypatch.setattr(gate, "push_frame", fake_push)

    async def run():
        lvl.hear(chunk(800))
        await gate.process_frame(
            TranscriptionFrame("volume up", "u", "0"), FrameDirection.DOWNSTREAM
        )
        await gate.process_frame(
            TranscriptionFrame("what time is it", "u", "0"), FrameDirection.DOWNSTREAM
        )

    monkeypatch.setattr(gate, "_run_intent", lambda *a: asyncio.sleep(0, result=True))
    asyncio.run(run())
    hit = glog.find("gate_match")[0]
    assert hit["level_db"] == -20.0 and "quiet_ms" in hit, hit
    miss = glog.find("gate_miss")[0]
    assert miss["level_db"] is None, "the second turn had no audio of its own"
