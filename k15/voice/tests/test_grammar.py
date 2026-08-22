"""Blind test: the Tier-1 grammar, offline - utterance -> intent+slots,
negatives that must fall through to the assistant lane. Run:
    .venv\\Scripts\\python tests\\test_grammar.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grammar_gate import GrammarMatcher, strip_wake

VOICE_CFG = {"inputs": {"apple tv": "hdmi1", "playstation": "hdmi2",
                        "ps5": "hdmi2", "the pc": "hdmi4"},
             "navTargets": {"downloads": "downloads", "the downloads": "downloads",
                            "library": "library", "my library": "library",
                            "store": "store", "the store": "store"}}

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
    # {game} is a wildcard carrying normalized spoken text; title->appid
    # resolution is titles.py, tested in test_library.
    ("play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("launch elden ring", "PlayGame", {"game": "elden ring"}),
    ("put on the game forza horizon five", "PlayGame", {"game": "forza horizon 5"}),
    ("start elden ring", "PlayGame", {"game": "elden ring"}),
    ("play some music", "PlayGame", {"game": "some music"}),
    # Conversational lead-ins: the commonest launch phrasings in the logs, and
    # they used to fall through to the LLM with an empty slot.
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
    ("cancel", "ExitSession", {}),          # bare cancel stays conversation-close
    # Safe to widen where EndSession is not: touches nothing in the room.
    ("go away", "ExitSession", {}),
    ("leave me alone", "ExitSession", {}),
    # Background-task surface - narrow by design.
    ("what did you find", "TaskResult", {}),
    ("what did you find out", "TaskResult", {}),
    ("any updates", "TaskResult", {}),
    ("any updates on the research", "TaskResult", {}),
    ("task update", "TaskResult", {}),
    ("give me the details", "TaskDetail", {}),
    ("task details", "TaskDetail", {}),
    ("cancel the task", "TaskCancel", {}),
    ("cancel the job", "TaskCancel", {}),
    # --- MUST fall through (assistant lane / no action) ----------------------
    ("what mech games do i have", None, {}),
    ("suggest a shooter i haven't played in a while", None, {}),
    ("hello there", None, {}),
    ("start", None, {}),
    ("play", None, {}),
    ("switch to the garage", None, {}),          # unknown input name
    ("show me deadlock", None, {}),              # a game name: no nav/collection
                                                 # marker -> assistant (game page)
    ("show me the pictures", None, {}),          # not a nav target -> assistant
    # Risky-command narrowness: casual variants must NOT end a session.
    ("end it", None, {}),
    ("stop", None, {}),
    ("kill the session please maybe", None, {}),
    ("exit", None, {}),                     # bare verb must not tear down the TV
    ("exit the game", None, {}),            # quitting a GAME is not ending the session
    ("end of session", None, {}),           # an STT mishear, deliberately not encoded
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
    ("hey jervis play hades", "play hades"),            # fuzzy mishear >= 80
    ("okay jarvis louder", "louder"),
    ("hey jarvis hey jarvis volume up", "volume up"),   # stutter/double wake
    ("hey jarvis", ""),
    ("Jarvis!", ""),
    ("volume up", "volume up"),
    ("travis strikes again", "travis strikes again"),   # real word ~67, kept
    ("hey volume up", "hey volume up"),                 # no anchor, untouched
    ("play jarvis game", "play jarvis game"),           # mid-text is content
    ("hey jar vis volume up", "volume up"),             # split anchor, joined
]

# Same stripper, "alfred" anchor: split mishears (2026-08-15), and the join
# staying under 80 for real phrases.
STRIP_ALFRED = [
    ("hey alfred volume up", "volume up"),
    ("Hey, all. Fred, take me home.", "take me home."),  # joined "allfred" ~92
    ("alfred play hades", "play hades"),
    ("all for one", "all for one"),                      # joined "allfor" ~67
]

# The two-token join, both directions: (text, anchor, want). The second group
# is held back only by _WHOLE_ANCHOR - each joins high enough to strip on its
# own ("a jarvis" 92.3, "my jarvis"/"is jarvis" 85.7, "the jarvis" exactly 80).
STRIP_JOIN = [
    ("hey al fred play hades", "alfred", "play hades"),
    ("al fred volume up", "alfred", "volume up"),
    ("hey al fred hey al fred stop", "alfred", "stop"),   # stutter, both split
    ("all frenzy games", "alfred", "all frenzy games"),   # joined ~67
    ("a jarvis skin for my avatar", "jarvis", "a jarvis skin for my avatar"),
    ("my jarvis mug broke", "jarvis", "my jarvis mug broke"),
    ("the jarvis file is missing", "jarvis", "the jarvis file is missing"),
    ("is jarvis working", "jarvis", "is jarvis working"),
]


def main():
    m = GrammarMatcher(VOICE_CFG)
    failures = []
    for text, want_intent, want_slots in TABLE:
        got = m.match(text)
        if want_intent is None:
            if got is not None:
                failures.append(f"'{text}': expected NO match, got {got}")
            continue
        if got is None:
            failures.append(f"'{text}': expected {want_intent}, got no match")
            continue
        intent, slots = got
        if intent != want_intent:
            failures.append(f"'{text}': expected {want_intent}, got {intent}")
            continue
        for k, v in want_slots.items():
            got_v = slots.get(k)
            if isinstance(v, (int, float)):
                ok = got_v is not None and float(got_v) == float(v)
            else:
                ok = str(got_v).lower() == str(v).lower()
            if not ok:
                failures.append(f"'{text}': slot {k}={got_v!r}, want {v!r}")

    for text, want in STRIP:
        got = strip_wake(text)
        if got != want:
            failures.append(f"strip '{text}': got {got!r}, want {want!r}")
    for text, want in STRIP_ALFRED:
        got = strip_wake(text, "alfred")
        if got != want:
            failures.append(f"strip '{text}': got {got!r}, want {want!r}")

    for text, anchor, want in STRIP_JOIN:
        got = strip_wake(text, anchor)
        if got != want:
            failures.append(f"strip[{anchor}] '{text}': got {got!r}, want {want!r}")
    # Strip output must still match the grammar.
    stripped = strip_wake("hey jarvis volume up")
    if m.match(stripped) is None or m.match(stripped)[0] != "VolumeUp":
        failures.append(f"stripped {stripped!r} no longer matches VolumeUp")

    # is_busy: an assistant turn in flight defers the idle timeout, but a hung
    # turn expires after ASSISTANT_WAIT_S so it can't pin the session open.
    import time as _t

    from grammar_gate import GrammarGate
    g = GrammarGate(m, None, lambda s: None)
    if g.is_busy():
        failures.append("fresh gate must not be busy")
    g._assistant_pending = _t.time()
    if not g.is_busy():
        failures.append("assistant turn in flight must defer idle")
    g._assistant_pending = _t.time() - (GrammarGate.ASSISTANT_WAIT_S + 1)
    if g.is_busy():
        failures.append("expired assistant turn must not pin the session")

    # The stop_listening tool ARMS the gate; the session ends only once the
    # goodbye is spoken, since the tool runs before the model has said a word.
    import asyncio

    import cglib
    from pipecat.frames.frames import (BotStoppedSpeakingFrame, EndWorkerFrame,
                                       ErrorFrame)
    from pipecat.processors.frame_processor import FrameDirection

    def drive(frames, arm):
        """Feed frames to a fresh gate with push_frame stubbed; return the
        EndWorkerFrames it pushed, and its log."""
        glog = cglib.CapturingLog("voice")
        gate = GrammarGate(m, None, glog)
        pushed = []

        async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        gate.push_frame = fake_push

        async def run():
            if arm:
                gate.request_stop()
            for f in frames:
                await gate.process_frame(f, FrameDirection.UPSTREAM)

        asyncio.run(run())
        return [f for f in pushed if isinstance(f, EndWorkerFrame)], glog

    ended, glog = drive([BotStoppedSpeakingFrame()], arm=True)
    if len(ended) != 1:
        failures.append(f"an armed stop must end the session exactly once, "
                        f"got {len(ended)}")
    if "session_stop_requested" not in glog.events():
        failures.append("arming the stop must log session_stop_requested")
    ended, _ = drive([BotStoppedSpeakingFrame()], arm=False)
    if ended:
        failures.append("finishing an ordinary answer must not end the session")
    # The goodbye can die between the model and the speaker; the ask stands.
    ended, _ = drive([ErrorFrame(error="synthetic tts failure")], arm=True)
    if len(ended) != 1:
        failures.append("a failed answer must still honour an armed stop")

    # An assistant turn is silent while it works: the only sounds are the
    # answer itself and, on error, the fail earcon.
    import earcons
    if "think" in earcons.SPECS:
        failures.append("the think earcon is back - it was removed on purpose")

    for f in failures:
        print("FAIL", f)
    assert not failures, f"{len(failures)} grammar failures"
    print(f"OK - {len(TABLE)} utterances: intents, slots, fall-throughs, "
          f"risky-command narrowness; "
          f"{len(STRIP) + len(STRIP_ALFRED) + len(STRIP_JOIN)} wake-strip "
          f"cases ({len(STRIP_JOIN)} two-token join); "
          f"is_busy defers for in-flight assistant turns; an armed stop ends "
          f"the session after the goodbye, never before")


if __name__ == "__main__":
    main()
