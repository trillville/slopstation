"""Test the room-level measurement behind level_db and quiet_ms, and the mic
gate that silences the room under the floor."""

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


def live(floor_db=0, log=None, loud=None, talker=8000):
    """A RoomLevel past the replay whose first live turn set the reference."""
    lvl = RoomLevel(floor_db=floor_db, log=log, loud=loud)
    lvl.go_live()
    lvl.hear(chunk(talker), now=0.0)
    lvl.snapshot(now=0.0)
    return lvl


def test_the_first_live_turn_is_the_reference_not_the_pre_roll():
    # The pre-roll was recorded before the duck: its peak is the un-ducked TV
    # (8000), the talker on the couch 20 dB under it. That must not be the
    # reference, or the gate would mute the person.
    lvl = RoomLevel(floor_db=15)
    lvl.hear(chunk(8000), now=0.0)  # replay: TV
    lvl.hear(chunk(800), now=0.1)  # replay: the wake phrase
    assert lvl.snapshot(now=0.2)["level_db"] is None, "no reference yet"
    assert lvl.reference == 0
    lvl.go_live()
    lvl.hear(chunk(800), now=1.0)  # the talker, TV now ducked
    assert lvl.snapshot(now=1.0)["level_db"] == 0.0
    assert lvl.reference == 800


def test_level_is_measured_against_the_talker():
    lvl = live()
    for _ in range(5):
        lvl.hear(chunk(800), now=2.0)  # chatter 20 dB down
    assert lvl.snapshot(now=2.0)["level_db"] == -20.0
    assert lvl.snapshot(now=3.0)["level_db"] is None, "an empty turn has no peak"


def test_quiet_ms_counts_from_the_talkers_last_loud_hop():
    lvl = live()
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


def test_the_gate_mutes_the_room_once_the_talker_goes_quiet():
    glog = CapturingLog("voice")
    lvl = live(floor_db=15, log=glog)  # floor ~1422 under an 8000 talker
    talk, tv, silence = chunk(8000), chunk(400), bytes(len(chunk(0)))
    assert lvl.hear(talk, now=1.0) == talk, "the talker always passes"
    assert lvl.hear(tv, now=1.1) == tv, "inside the 350 ms gap the room still passes"
    assert lvl.hear(tv, now=1.4) == silence, "past the gap the room is muted"
    assert lvl.gated
    assert lvl.hear(tv, now=3.0) == silence
    # The first hop back near the floor reopens the gate at once.
    assert lvl.hear(talk, now=3.1) == talk and not lvl.gated
    gated = glog.find("mic_gated")
    assert len(gated) == 1 and gated[0]["gated_ms"] == 1700, gated
    assert gated[0]["peak_db"] == -26.0, "the loudest thing silenced, vs the talker"


def test_the_gate_reopens_on_a_soft_onset_and_logs_only_long_mutes():
    glog = CapturingLog("voice")
    lvl = live(floor_db=15, log=glog)  # floor 1422, reopens from 711
    lvl.hear(chunk(8000), now=1.0)
    lvl.hear(chunk(100), now=1.4)
    assert lvl.gated
    onset = chunk(900)  # the consonant before the vowel: under the floor
    assert lvl.hear(onset, now=1.5) == onset and not lvl.gated
    assert not glog.find("mic_gated"), "a 100 ms mute is a gap between words"


def test_no_floor_no_reference_or_a_loud_room_never_mutes():
    tv = chunk(400)
    off = live(floor_db=0)
    assert off.hear(tv, now=5.0) == tv and not off.gated
    unknown = RoomLevel(floor_db=15)  # nothing live yet
    unknown.hear(chunk(8000), now=1.0)
    assert unknown.hear(tv, now=5.0) == tv and not unknown.gated
    loud = live(floor_db=15, loud=lambda: True)  # the duck did not land
    assert loud.hear(tv, now=5.0) == tv and not loud.gated


def test_the_gate_stamps_every_transcript_with_the_room(monkeypatch):
    lvl = live()
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
    assert hit["level_db"] == -20.0, hit
    assert hit["quiet_ms"] is None or isinstance(hit["quiet_ms"], int), hit
    miss = glog.find("gate_miss")[0]
    assert miss["level_db"] is None, "the second turn had no audio of its own"
