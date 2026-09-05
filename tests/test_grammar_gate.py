"""Test offline grammar matching and assistant fallthrough."""

import asyncio
import dataclasses
import time

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    ErrorFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from helpers import CapturingLog
from slopstation.agent.speech.grammar_gate import (
    GrammarGate,
    GrammarMatcher,
    closer_in,
    load_closers,
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
    # The pre-roll caught a sentence in progress (session 82abe2): a greeted
    # anchor mid-text is where the user started addressing the room.
    ("what I mean. Hey, Alfred. What time is it?", "What time is it?"),
    # A loud room carried two attempts (session 84aa98): the last one counts.
    (
        "that's who you are. Hey, Alfred. What's up. Hey, Alfred. What time is it?",
        "What time is it?",
    ),
    ("tell my alfred story", "tell my alfred story"),  # bare mid-anchor: content
]

# closer_in: (text, expected closer or None), anchor "alfred".
CLOSERS = [
    ("Alright. Thanks.", "thanks"),  # fillers around it
    ("Thank.", "thanks"),  # mishear, ~91
    ("never mind, cancel", "cancel"),
    ("yeah leave me alone please", "leave me alone"),
    ("Okay. Thanks. Go ahead.", None),  # the tail is not a closer
    ("what time is it, thanks", None),  # long: wants its answer first
    ("cancel the download", None),  # a closer at the head is content
    ("The Alfred go away. Only hands exactly.", "go away"),  # after the anchor
    ("hey alfred what time is it", None),
    ("", None),
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


@pytest.mark.parametrize("text,want", CLOSERS, ids=[t[0] or "empty" for t in CLOSERS])
def test_closer_in_finds_a_closing_phrase_with_company(text, want):
    assert closer_in(text, load_closers(), "alfred") == want


def test_exit_sentences_stay_plain_for_closer_matching():
    for c in load_closers():
        assert not any(ch in c for ch in "[]{}|"), c


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


# The stop_listening tool runs on a worker thread; the gate ends the session
# on the very next frame through it, whatever that frame is, and nothing
# said after the ask gets through.


def test_a_stop_ends_the_session_on_the_next_frame(drive):
    ended, glog, _ = drive([BotStoppedSpeakingFrame(), ErrorFrame(error="x")], arm=True)
    assert len(ended) == 1, f"a stop must end the session exactly once, got {ended}"
    assert "session_stop_requested" in glog.events()


def test_an_ordinary_answer_does_not_end_the_session(drive):
    ended, _, _ = drive([BotStoppedSpeakingFrame()], arm=False)
    assert not ended, "finishing an ordinary answer must not end the session"


class FakeDispatch:
    def begin_utterance(self, turn, text):
        pass


@pytest.fixture
def hear(matcher, monkeypatch):
    """Feed transcripts to a gate with an "alfred" wake word; returns
    (EndWorkerFrames pushed, log, gate)."""

    def _hear(texts, loud=None, stop_first=False):
        glog = CapturingLog("voice")
        gate = GrammarGate(matcher, FakeDispatch(), glog, wake_word="alfred", loud=loud)
        pushed = []

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        monkeypatch.setattr(gate, "push_frame", fake_push)

        async def run():
            if stop_first:
                gate.request_stop()
            for t in texts:
                await gate.process_frame(
                    TranscriptionFrame(t, "u", "0"), FrameDirection.DOWNSTREAM
                )

        asyncio.run(run())
        return [f for f in pushed if isinstance(f, EndWorkerFrame)], glog, gate

    return _hear


def test_a_transcript_after_a_stop_is_dropped(hear):
    ended, glog, _ = hear(["What time is it?"], stop_first=True)
    assert len(ended) == 1
    assert glog.find("stt_final")[0]["outcome"] == "after_stop"
    assert not glog.find("gate_miss"), "a dropped transcript reached the assistant"


def test_a_closer_with_company_ends_the_session(hear):
    ended, glog, _ = hear(["Alright. Thanks."])
    assert len(ended) == 1, "a trailing closer must end the session"
    hit = glog.find("gate_match")[0]
    assert hit["intent"] == "ExitSession" and hit["closer"] == "thanks", hit
    assert "session_exit_phrase" in glog.events()


def test_a_loud_room_needs_the_wake_prefix_on_every_turn(hear):
    ended, glog, _ = hear(
        ["Hey Alfred, what time is it?", "enough to blow up the whole planet."],
        loud=lambda: True,
    )
    assert not ended
    heard = [r["text"] for r in glog.find("gate_miss")]
    assert heard == ["what time is it?"], heard
    dropped = glog.find("stt_final")
    assert len(dropped) == 1 and dropped[0]["outcome"] == "unaddressed", dropped


def test_a_quiet_room_hears_every_turn(hear):
    _, glog, _ = hear(["What time is it?"], loud=lambda: False)
    assert [r["text"] for r in glog.find("gate_miss")] == ["What time is it?"]


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
