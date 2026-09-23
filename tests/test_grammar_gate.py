"""Test offline grammar matching and assistant fallthrough."""

import asyncio
import dataclasses
import time

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
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
    ("game time", "StartSession", {}),
    ("let's play", "StartSession", {}),
    ("end the session", "EndSession", {}),
    # "exit ..." are statements of intent nobody says by accident; the mishears
    # beside them in the logs ("end of session", "access session") stay out.
    ("exit session", "EndSession", {}),
    ("exit gaming mode", "EndSession", {}),
    ("we're done", "EndSession", {}),
    ("volume up", "VolumeUp", {}),
    ("turn it up", "VolumeUp", {}),
    ("louder", "VolumeUp", {}),
    ("volume down", "VolumeDown", {}),
    ("quieter", "VolumeDown", {}),
    ("set the volume to 25", "VolumeSet", {"level": 25}),
    ("volume 30", "VolumeSet", {"level": 30}),
    ("set volume to 100", "VolumeSet", {"level": 100}),
    ("mute", "MuteToggle", {}),
    ("unmute the sound", "MuteToggle", {}),
    ("switch to the apple tv", "SwitchInput", {"input": "apple tv"}),
    ("go back to the playstation", "SwitchInput", {"input": "playstation"}),
    ("show the apple tv", "SwitchInput", {"input": "apple tv"}),
    # {target}'s value is the nav kind, and its vocabulary is disjoint from
    # {input}, so these never cross with SwitchInput.
    ("show downloads", "Nav", {"target": "downloads"}),
    ("open the store", "Nav", {"target": "store"}),
    ("go to my library", "Nav", {"target": "library"}),
    ("take me to downloads", "Nav", {"target": "downloads"}),
    # Polite lead-in: widens nothing, {target} is still an exact list.
    ("can you show me the downloads", "Nav", {"target": "downloads"}),
    # ShowCollection: wildcard resolved on the box; the "my"/"collection"
    # marker keeps a bare "show me <game>" out.
    ("show my roguelikes", "ShowCollection", {"collection": "roguelikes"}),
    ("show me the co-op collection", "ShowCollection", {"collection": "co op"}),
    ("open my mech games collection", "ShowCollection", {"collection": "mech games"}),
    # The {game} wildcard contains the spoken title.
    ("play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("put on the game forza horizon five", "PlayGame", {"game": "forza horizon 5"}),
    ("start elden ring", "PlayGame", {"game": "elden ring"}),
    ("play some music", "PlayGame", {"game": "some music"}),
    # Conversational lead-ins: the commonest launch phrasings in the logs.
    ("i want to play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("open armored core six", "PlayGame", {"game": "armored core 6"}),
    ("let's play elden ring", "PlayGame", {"game": "elden ring"}),
    ("can you play elden ring", "PlayGame", {"game": "elden ring"}),
    ("wanna launch elden ring", "PlayGame", {"game": "elden ring"}),
    # "can you START x" is deliberately not a PlayGame form: StartSession has
    # no polite variant to claim it first, so it matched game="the session".
    ("can you start the session", None, {}),
    ("thanks", "ExitSession", {}),
    ("cancel", "ExitSession", {}),  # bare cancel stays conversation-close
    # Safe to widen where EndSession is not: touches nothing in the room.
    ("go away", "ExitSession", {}),
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
# phrase: (transcript, anchor, what the lanes should see; "" = swallowed).
STRIP = [
    ("hey jarvis volume up", "jarvis", "volume up"),
    ("Hey, Jarvis, volume up.", "jarvis", "volume up."),
    ("jarvis volume up", "jarvis", "volume up"),
    ("hey jervis play hades", "jarvis", "play hades"),  # fuzzy mishear >= 80
    ("okay jarvis louder", "jarvis", "louder"),
    ("hey jarvis hey jarvis volume up", "jarvis", "volume up"),  # double wake
    ("hey jarvis", "jarvis", ""),
    ("Jarvis!", "jarvis", ""),
    ("volume up", "jarvis", "volume up"),
    ("travis strikes again", "jarvis", "travis strikes again"),  # ~67, kept
    ("hey volume up", "jarvis", "hey volume up"),  # no anchor, untouched
    ("play jarvis game", "jarvis", "play jarvis game"),  # mid-text is content
    ("hey jar vis volume up", "jarvis", "volume up"),  # split anchor, joined
    # "alfred": split mishears, and the join staying under 80 for real phrases.
    ("Hey, all. Fred, take me home.", "alfred", "take me home."),  # ~92 joined
    ("all for one", "alfred", "all for one"),  # joined "allfor" ~67
    # The pre-roll caught a sentence in progress.
    ("what I mean. Hey, Alfred. What time is it?", "alfred", "What time is it?"),
    # A loud room carried two attempts: the last one counts.
    (
        "that's who you are. Hey, Alfred. What's up. Hey, Alfred. What time is it?",
        "alfred",
        "What time is it?",
    ),
    # A greeted anchor with nothing after it is not where a command starts.
    ("hey alfred play hades hey alfred", "alfred", "play hades hey alfred"),
    ("is that okay alfred", "alfred", "is that okay alfred"),
    ("what  time is it", "alfred", "what  time is it"),  # nothing touched
    # The two-token join. The last three are held back only by _WHOLE_ANCHOR -
    # each joins high enough to strip on its own ("a jarvis" 92.3, "my jarvis"
    # 85.7, "the jarvis" exactly 80).
    ("al fred volume up", "alfred", "volume up"),
    ("hey al fred hey al fred stop", "alfred", "stop"),  # stutter, both split
    ("all frenzy games", "alfred", "all frenzy games"),  # joined ~67
    ("a jarvis skin for my avatar", "jarvis", "a jarvis skin for my avatar"),
    ("my jarvis mug broke", "jarvis", "my jarvis mug broke"),
    ("the jarvis file is missing", "jarvis", "the jarvis file is missing"),
]

# closer_in: (text, expected closer or None), anchor "alfred", quiet room.
CLOSERS = [
    ("Alright. Thanks.", "thanks"),  # fillers around it
    ("Thank.", "thanks"),  # the one listed mishear
    ("thanks alfred", "thanks"),  # the anchor is not content either
    ("no thanks", "thanks"),
    ("alright, that's all", "that's all"),  # "all" is the closer's own word
    ("all right, that's all", "that's all"),
    ("don't go away", None),  # the opposite of a closer
    ("please don't go away", None),
    ("never mind, cancel", "cancel"),
    ("yeah leave me alone please", "leave me alone"),
    ("Okay. Thanks. Go ahead.", None),  # the tail is not a closer
    ("what time is it, thanks", None),  # long: wants its answer first
    ("what's the weather alfred thanks", None),  # same, anchor or not
    ("cancel the download", None),  # a closer at the head is content
    ("actually alfred cancel the download", None),  # still a command
    ("world of tanks", None),  # a title is an answer, not "thanks"
    ("The Alfred go away. Only hands exactly.", None),  # quiet room: content
    ("hey alfred what time is it", None),
    ("", None),
]

# A loud room: the TV finishes the sentence, so a closer after the anchor counts.
CLOSERS_LOUD = [
    ("The Alfred go away. Only hands exactly.", "go away"),
    ("Hey Alfred go away only hands exactly", "go away"),  # as heard, unstripped
    ("Hey Alfred don't go away", None),
    ("actually alfred cancel the download", "cancel"),  # the price of it
    ("what time is it, thanks", None),
]


@pytest.fixture
def matcher():
    return GrammarMatcher(VOICE_CFG)


class FakeDispatch:
    def begin_utterance(self, turn, text):
        pass


@pytest.fixture
def drive(matcher, monkeypatch):
    """Feed frames (a string is a final transcript) to a fresh gate with an
    "alfred" wake word and push_frame stubbed; returns the EndWorkerFrames it
    pushed, its log, and the gate."""

    def _drive(frames, stop_first=False, **gate_kw):
        glog = CapturingLog("voice")
        gate = GrammarGate(matcher, FakeDispatch(), glog, wake_word="alfred", **gate_kw)
        pushed = []

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        monkeypatch.setattr(gate, "push_frame", fake_push)

        async def run():
            if stop_first:
                gate.request_stop()
            for f in frames:
                if isinstance(f, str):
                    f = TranscriptionFrame(f, "u", "0")
                await gate.process_frame(f, FrameDirection.DOWNSTREAM)

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


@pytest.mark.parametrize(
    "text,anchor,want", STRIP, ids=[f"{t[1]}:{t[0]}" for t in STRIP]
)
def test_strip_wake(text, anchor, want):
    assert strip_wake(text, anchor) == want


@pytest.mark.parametrize("text,want", CLOSERS, ids=[t[0] or "empty" for t in CLOSERS])
def test_closer_in_finds_a_closing_phrase_with_company(text, want):
    assert closer_in(text, GrammarMatcher(VOICE_CFG).closers, "alfred") == want


@pytest.mark.parametrize("text,want", CLOSERS_LOUD, ids=[t[0] for t in CLOSERS_LOUD])
def test_a_loud_room_takes_a_closer_right_after_the_anchor(text, want):
    closers = GrammarMatcher(VOICE_CFG).closers
    assert closer_in(text, closers, "alfred", loud=True) == want


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


async def test_busy_phrase_does_not_read_as_the_answer(matcher, monkeypatch):
    """The busy phrase speaks before the answer, so the frame it raises must
    not clear the in-flight turn - that is what closed a session mid-answer."""
    g = GrammarGate(matcher, None, lambda s: None)

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    monkeypatch.setattr(g, "push_frame", fake_push)
    monkeypatch.setattr(g, "_assistant_pending", time.time())
    g.expect_filler()
    await g.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert g.is_busy(), "the phrase is not the answer; the turn is still in flight"
    await g.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert not g.is_busy(), "the answer must hand the session back to the idle clock"

    # Talk-over drops the phrase before it speaks, so the mark must not
    # survive into the next turn and swallow that turn's answer.
    g.expect_filler()
    await g.process_frame(UserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    monkeypatch.setattr(g, "_assistant_pending", time.time())
    monkeypatch.setattr(g, "_speaking", 0.0)
    await g.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert not g.is_busy(), "a dropped phrase must not pin the next turn open"


# stop_listening runs off-thread; the gate ends the session on the next
# frame through it, and nothing said after the ask gets through.


def test_a_stop_ends_the_session_on_the_next_frame(drive):
    ended, glog, _ = drive(
        [BotStoppedSpeakingFrame(), ErrorFrame(error="x")], stop_first=True
    )
    assert len(ended) == 1, f"a stop must end the session exactly once, got {ended}"
    assert "session_stop_requested" in glog.events()


def test_a_transcript_after_a_stop_is_dropped(drive):
    ended, glog, _ = drive(["What time is it?"], stop_first=True)
    assert len(ended) == 1
    assert glog.find("turn_dropped")[0]["reason"] == "after_stop"
    assert not glog.find("gate_miss"), "a dropped transcript reached the assistant"


def test_a_closer_with_company_ends_the_session(drive):
    ended, glog, _ = drive(["Hey Alfred. Alright. Thanks."])
    assert len(ended) == 1, "a trailing closer must end the session"
    hit = glog.find("gate_match")[0]
    assert hit["intent"] == "ExitSession" and hit["closer"] == "thanks", hit
    assert "session_exit_phrase" in glog.events()


def test_a_loud_room_needs_the_wake_prefix_on_every_turn(drive):
    ended, glog, _ = drive(
        ["Hey Alfred, what time is it?", "enough to blow up the whole planet."],
        loud=lambda: True,
    )
    assert not ended
    heard = [r["text"] for r in glog.find("gate_miss")]
    assert heard == ["what time is it?"], heard
    dropped = glog.find("turn_dropped")
    assert len(dropped) == 1 and dropped[0]["reason"] == "unaddressed", dropped


@pytest.mark.parametrize(
    "text",
    ["Hey Alfred go away only hands exactly", "The Alfred go away only hands exactly"],
)
def test_a_loud_room_closes_on_go_away_however_the_tv_finished_it(drive, text):
    ended, glog, _ = drive([text], loud=lambda: True)
    assert len(ended) == 1, glog.records
    assert glog.find("gate_match")[0]["closer"] == "go away"
    assert not glog.find("turn_dropped"), "addressed, so never unaddressed"


def test_a_quiet_room_hears_every_turn(drive):
    ended, glog, _ = drive(
        ["Hey Alfred, what time is it?", "and tomorrow?"], loud=lambda: False
    )
    assert not ended
    heard = [r["text"] for r in glog.find("gate_miss")]
    assert heard == ["what time is it?", "and tomorrow?"], heard


def test_a_wake_nobody_said_ends_the_session(drive):
    ended, glog, _ = drive(["enough to blow up the whole planet."])
    assert len(ended) == 1, "the TV woke it: nobody is talking to it"
    assert glog.find("turn_dropped")[0]["reason"] == "false_wake"
    assert not glog.find("gate_miss"), "the TV reached the assistant"


@pytest.mark.parametrize("wake", ["Hey Alfred.", "What I mean. Hey Alfred."])
def test_a_pause_style_wake_is_still_a_wake(drive, wake):
    # The pre-roll can hold a sentence in progress before the wake phrase.
    ended, glog, _ = drive([wake, "What time is it?"])
    assert not ended, glog.records
    assert "What time is it?" in [r["text"] for r in glog.find("gate_miss")]


def test_a_follow_up_open_needs_no_wake_word(drive):
    ended, glog, _ = drive(["What time is it?"], addressed=True)
    assert not ended
    assert [r["text"] for r in glog.find("gate_miss")] == ["What time is it?"]


def test_turn_edges_defer_idle_claim_the_chime_and_expire(drive, monkeypatch):
    """Turn edges (the resolver's real frames): busy mid-turn defers the idle
    handler, the stop claims the chime, and the flag EXPIRES - a Flux socket
    that dies mid-turn never sends the stop edge and must not pin the
    session open."""
    ack = WakeAck()
    _, _, gate = drive([UserStartedSpeakingFrame()], ack=ack)
    assert gate.is_busy(), "an open user turn must read as mid-turn"
    monkeypatch.setattr(
        gate, "_speaking", time.time() - (GrammarGate.SPEAKING_WAIT_S + 1)
    )
    assert not gate.is_busy(), "a lost stop edge must not pin the session open"
    _, _, gate = drive(
        [UserStartedSpeakingFrame(), UserStoppedSpeakingFrame()], ack=ack
    )
    assert not gate.is_busy(), "a closed user turn must not read as mid-turn"
    assert not ack.claim(), "the turn stop must claim the wake chime"
