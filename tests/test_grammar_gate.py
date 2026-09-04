"""Test offline grammar matching and assistant fallthrough."""

import asyncio
import dataclasses
import time

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    ErrorFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from helpers import CapturingLog
from slopstation.agent.speech.grammar_gate import (
    GrammarGate,
    GrammarMatcher,
    strip_wake,
    stt_confidence,
)
from slopstation.agent.speech.preroll import WakeAck

VOICE_CFG = {
    "inputs": {
        "apple tv": "hdmi1",
        "playstation": "hdmi2",
        "ps5": "hdmi2",
        "the pc": "hdmi4",
    },
    "navTargets": {
        "downloads": "downloads",
        "the downloads": "downloads",
        "library": "library",
        "my library": "library",
        "store": "store",
        "the store": "store",
    },
}

# (utterance, expected intent or None, expected slots subset)
TABLE = [
    ("start a session", "StartSession", {}),
    ("start the gaming session", "StartSession", {}),
    ("game time", "StartSession", {}),
    ("let's play", "StartSession", {}),
    ("end the session", "EndSession", {}),
    ("end session", "EndSession", {}),
    # "exit ..." are statements of intent nobody says by accident; the mishears
    # beside them in the logs ("end of session", "access session") stay out.
    ("exit session", "EndSession", {}),
    ("exit the gaming session", "EndSession", {}),
    ("exit gaming mode", "EndSession", {}),
    ("exit tv mode", "EndSession", {}),
    ("we're done", "EndSession", {}),
    ("we're done gaming", "EndSession", {}),
    ("volume up", "VolumeUp", {}),
    ("turn the volume up", "VolumeUp", {}),
    ("turn it up", "VolumeUp", {}),
    ("louder", "VolumeUp", {}),
    ("volume down", "VolumeDown", {}),
    ("quieter", "VolumeDown", {}),
    ("set the volume to 25", "VolumeSet", {"level": 25}),
    ("volume 30", "VolumeSet", {"level": 30}),
    ("set volume to 100", "VolumeSet", {"level": 100}),
    ("mute", "MuteToggle", {}),
    ("mute the tv", "MuteToggle", {}),
    ("unmute the sound", "MuteToggle", {}),
    ("switch to the apple tv", "SwitchInput", {"input": "apple tv"}),
    ("go back to the playstation", "SwitchInput", {"input": "playstation"}),
    ("switch to ps5", "SwitchInput", {"input": "ps5"}),
    ("show the apple tv", "SwitchInput", {"input": "apple tv"}),
    # {target}'s value is the nav kind, and its vocabulary is disjoint from
    # {input}, so these never cross with SwitchInput.
    ("show downloads", "Nav", {"target": "downloads"}),
    ("show me the downloads", "Nav", {"target": "downloads"}),
    ("open the store", "Nav", {"target": "store"}),
    ("go to my library", "Nav", {"target": "library"}),
    ("take me to downloads", "Nav", {"target": "downloads"}),
    # Polite lead-in: widens nothing, {target} is still an exact list.
    ("can you show me the downloads", "Nav", {"target": "downloads"}),
    ("could you open the store", "Nav", {"target": "store"}),
    ("can you go to my library", "Nav", {"target": "library"}),
    # ShowCollection: wildcard resolved on the box; the "my"/"collection"
    # marker keeps a bare "show me <game>" out.
    ("show my roguelikes", "ShowCollection", {"collection": "roguelikes"}),
    ("show me the co-op collection", "ShowCollection", {"collection": "co op"}),
    ("open my mech games collection", "ShowCollection", {"collection": "mech games"}),
    # The {game} wildcard contains the spoken title.
    ("play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("launch elden ring", "PlayGame", {"game": "elden ring"}),
    ("put on the game forza horizon five", "PlayGame", {"game": "forza horizon 5"}),
    ("start elden ring", "PlayGame", {"game": "elden ring"}),
    ("play some music", "PlayGame", {"game": "some music"}),
    # Conversational lead-ins: the commonest launch phrasings in the logs.
    ("i want to play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("i wanna play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("i would like to play elden ring", "PlayGame", {"game": "elden ring"}),
    ("open armored core six", "PlayGame", {"game": "armored core 6"}),
    ("let's play elden ring", "PlayGame", {"game": "elden ring"}),
    ("can you play elden ring", "PlayGame", {"game": "elden ring"}),
    ("wanna launch elden ring", "PlayGame", {"game": "elden ring"}),
    # "can you START x" is deliberately not a PlayGame form: StartSession has
    # no polite variant to claim it first, so it matched game="the session".
    ("can you start the session", None, {}),
    ("thanks", "ExitSession", {}),
    ("that's all", "ExitSession", {}),
    ("never mind", "ExitSession", {}),
    ("cancel", "ExitSession", {}),  # bare cancel stays conversation-close
    # Safe to widen where EndSession is not: touches nothing in the room.
    ("go away", "ExitSession", {}),
    ("leave me alone", "ExitSession", {}),
    # --- MUST fall through (assistant lane / no action) ----------------------
    ("what mech games do i have", None, {}),
    ("suggest a shooter i haven't played in a while", None, {}),
    ("hello there", None, {}),
    ("start", None, {}),
    ("play", None, {}),
    ("switch to the garage", None, {}),  # unknown input name
    # a game name: no nav/collection marker -> assistant (game page)
    ("show me deadlock", None, {}),
    ("show me the pictures", None, {}),  # not a nav target -> assistant
    # Risky-command narrowness: casual variants must NOT end a session.
    ("end it", None, {}),
    ("stop", None, {}),
    ("kill the session please maybe", None, {}),
    ("exit", None, {}),  # bare verb must not tear down the TV
    ("exit the game", None, {}),  # quitting a GAME is not ending the session
    ("end of session", None, {}),  # an STT mishear, deliberately not encoded
    ("go", None, {}),
    # Conversational phrasings stay in the assistant lane.
    ("tell me more", None, {}),
    ("what did you find in the garage", None, {}),
]

# Wake-prefix stripping, since pre-roll makes transcripts start with the wake
# phrase: (transcript, what the lanes should see; "" = swallowed entirely).
STRIP = [
    ("hey jarvis volume up", "volume up"),
    ("Hey, Jarvis, volume up.", "volume up."),
    ("jarvis volume up", "volume up"),
    ("hey jervis play hades", "play hades"),  # fuzzy mishear >= 80
    ("okay jarvis louder", "louder"),
    ("hey jarvis hey jarvis volume up", "volume up"),  # stutter/double wake
    ("hey jarvis", ""),
    ("Jarvis!", ""),
    ("volume up", "volume up"),
    ("travis strikes again", "travis strikes again"),  # real word ~67, kept
    ("hey volume up", "hey volume up"),  # no anchor, untouched
    ("play jarvis game", "play jarvis game"),  # mid-text is content
    ("hey jar vis volume up", "volume up"),  # split anchor, joined
]

# Same stripper, "alfred" anchor: split mishears, and the join
# staying under 80 for real phrases.
STRIP_ALFRED = [
    ("hey alfred volume up", "volume up"),
    ("Hey, all. Fred, take me home.", "take me home."),  # joined "allfred" ~92
    ("alfred play hades", "play hades"),
    ("all for one", "all for one"),  # joined "allfor" ~67
]

# The two-token join, both directions: (text, anchor, want). The second group
# is held back only by _WHOLE_ANCHOR - each joins high enough to strip on its
# own ("a jarvis" 92.3, "my jarvis"/"is jarvis" 85.7, "the jarvis" exactly 80).
STRIP_JOIN = [
    ("hey al fred play hades", "alfred", "play hades"),
    ("al fred volume up", "alfred", "volume up"),
    ("hey al fred hey al fred stop", "alfred", "stop"),  # stutter, both split
    ("all frenzy games", "alfred", "all frenzy games"),  # joined ~67
    ("a jarvis skin for my avatar", "jarvis", "a jarvis skin for my avatar"),
    ("my jarvis mug broke", "jarvis", "my jarvis mug broke"),
    ("the jarvis file is missing", "jarvis", "the jarvis file is missing"),
    ("is jarvis working", "jarvis", "is jarvis working"),
]


@pytest.fixture
def matcher():
    return GrammarMatcher(VOICE_CFG)


@pytest.fixture
def drive(matcher, monkeypatch):
    """Feed frames to a fresh gate with push_frame stubbed; returns the
    EndWorkerFrames it pushed, its log, and the gate."""

    def _drive(frames, arm, ack=None):
        glog = CapturingLog("voice")
        gate = GrammarGate(matcher, None, glog, ack=ack)
        pushed = []

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        monkeypatch.setattr(gate, "push_frame", fake_push)

        async def run():
            if arm:
                gate.request_stop()
            for f in frames:
                await gate.process_frame(f, FrameDirection.UPSTREAM)

        asyncio.run(run())
        return [f for f in pushed if isinstance(f, EndWorkerFrame)], glog, gate

    return _drive


@pytest.mark.parametrize(
    "text,want_intent,want_slots", TABLE, ids=[t[0] for t in TABLE]
)
def test_utterance_maps_to_intent_and_slots(matcher, text, want_intent, want_slots):
    got = matcher.match(text)
    if want_intent is None:
        assert got is None, f"'{text}': expected NO match, got {got}"
        return
    assert got is not None, f"'{text}': expected {want_intent}, got no match"
    intent, slots = got
    assert intent == want_intent, f"'{text}': expected {want_intent}, got {intent}"
    for k, v in want_slots.items():
        got_v = slots.get(k)
        if isinstance(v, (int, float)):
            ok = got_v is not None and float(got_v) == float(v)
        else:
            ok = str(got_v).lower() == str(v).lower()
        assert ok, f"'{text}': slot {k}={got_v!r}, want {v!r}"


@pytest.mark.parametrize("text,want", STRIP, ids=[t[0] for t in STRIP])
def test_strip_wake_removes_the_jarvis_prefix(text, want):
    assert strip_wake(text) == want


@pytest.mark.parametrize("text,want", STRIP_ALFRED, ids=[t[0] for t in STRIP_ALFRED])
def test_strip_wake_joins_a_split_alfred(text, want):
    assert strip_wake(text, "alfred") == want


@pytest.mark.parametrize(
    "text,anchor,want", STRIP_JOIN, ids=[f"{t[1]}:{t[0]}" for t in STRIP_JOIN]
)
def test_strip_wake_two_token_join(text, anchor, want):
    assert strip_wake(text, anchor) == want


@dataclasses.dataclass
class Frame:
    result: object


def test_confidence_is_read_and_never_raises():
    assert (
        stt_confidence(Frame({"words": [{"confidence": 0.9}, {"confidence": 0.7}]}))
        == 0.8
    )
    for bad in ({}, {"words": []}, {"words": [{}]}, {"words": "nope"}, None):
        assert stt_confidence(Frame(bad)) is None, bad

    class Exploding:
        @property
        def result(self):
            raise RuntimeError("upstream moved")

    assert stt_confidence(Exploding()) is None, "telemetry cost the turn"


def test_is_busy_defers_idle_until_the_assistant_turn_expires(matcher, monkeypatch):
    """An assistant turn in flight defers the idle timeout, but a hung turn
    expires after ASSISTANT_WAIT_S so it can't pin the session open."""
    g = GrammarGate(matcher, None, lambda s: None)
    assert not g.is_busy(), "fresh gate must not be busy"
    monkeypatch.setattr(g, "_assistant_pending", time.time())
    assert g.is_busy(), "assistant turn in flight must defer idle"
    monkeypatch.setattr(
        g, "_assistant_pending", time.time() - (GrammarGate.ASSISTANT_WAIT_S + 1)
    )
    assert not g.is_busy(), "expired assistant turn must not pin the session"


# The stop_listening tool ARMS the gate; the session ends only once the
# goodbye is spoken, since the tool runs before the model has said a word.


def test_an_armed_stop_ends_the_session_once_the_goodbye_is_spoken(drive):
    ended, glog, _ = drive([BotStoppedSpeakingFrame()], arm=True)
    assert len(ended) == 1, (
        f"an armed stop must end the session exactly once, got {len(ended)}"
    )
    assert "session_stop_requested" in glog.events(), (
        "arming the stop must log session_stop_requested"
    )


def test_an_ordinary_answer_does_not_end_the_session(drive):
    ended, _, _ = drive([BotStoppedSpeakingFrame()], arm=False)
    assert not ended, "finishing an ordinary answer must not end the session"


def test_a_failed_answer_still_honours_an_armed_stop(drive):
    # The goodbye can die between the model and the speaker; the ask stands.
    ended, _, _ = drive([ErrorFrame(error="synthetic tts failure")], arm=True)
    assert len(ended) == 1, "a failed answer must still honour an armed stop"


def test_turn_edges_defer_idle_claim_the_chime_and_expire(drive, monkeypatch):
    """Turn edges (the resolver's real frames): busy mid-turn defers the idle
    handler, the stop claims the chime, and the flag EXPIRES - a Flux socket
    that dies mid-turn never sends the stop edge and must not pin the
    session open."""
    ack = WakeAck()
    _, _, gate = drive([UserStartedSpeakingFrame()], arm=False, ack=ack)
    assert gate.is_busy(), "an open user turn must read as mid-turn"
    monkeypatch.setattr(
        gate, "_speaking", time.time() - (GrammarGate.SPEAKING_WAIT_S + 1)
    )
    assert not gate.is_busy(), "a lost stop edge must not pin the session open"
    _, _, gate = drive(
        [UserStartedSpeakingFrame(), UserStoppedSpeakingFrame()], arm=False, ack=ack
    )
    assert not gate.is_busy(), "a closed user turn must not read as mid-turn"
    assert not ack.claim(), "the turn stop must claim the wake chime"
