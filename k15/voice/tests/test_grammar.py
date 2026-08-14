"""Blind test: the Tier-1 grammar, offline - utterance -> intent+slots
table, negatives that MUST fall through to the assistant lane, and the
risky-command narrowness rule. Run:
    .venv\\Scripts\\python tests\\test_grammar.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from grammar_gate import GrammarMatcher, strip_wake

VOICE_CFG = {"inputs": {"apple tv": "hdmi1", "playstation": "hdmi2",
                        "ps5": "hdmi2", "the pc": "hdmi4"}}

# (utterance, expected intent or None, expected slots subset)
TABLE = [
    ("start a session", "StartSession", {}),
    ("start the gaming session", "StartSession", {}),
    ("game time", "StartSession", {}),
    ("let's play", "StartSession", {}),
    ("end the session", "EndSession", {}),
    ("end session", "EndSession", {}),
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
    # {game} is a wildcard: the slot carries the (normalized) spoken text;
    # title->appid resolution is titles.py's job, tested in test_library.
    ("play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("launch elden ring", "PlayGame", {"game": "elden ring"}),
    ("put on the game forza horizon five", "PlayGame", {"game": "forza horizon 5"}),
    ("start elden ring", "PlayGame", {"game": "elden ring"}),
    ("play some music", "PlayGame", {"game": "some music"}),
    ("thanks", "ExitSession", {}),
    ("that's all", "ExitSession", {}),
    ("never mind", "ExitSession", {}),
    ("cancel", "ExitSession", {}),          # bare cancel stays conversation-close
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
    # Risky-command narrowness: casual variants must NOT end a session.
    ("end it", None, {}),
    ("stop", None, {}),
    ("kill the session please maybe", None, {}),
    # Conversational phrasings stay in the assistant lane.
    ("tell me more", None, {}),
    ("what did you find in the garage", None, {}),
]

# Wake-prefix stripping (pre-roll makes transcripts start with the wake
# phrase): (transcript, what the lanes should see; "" = swallowed entirely).
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
    # Strip output must still match the grammar.
    stripped = strip_wake("hey jarvis volume up")
    if m.match(stripped) is None or m.match(stripped)[0] != "VolumeUp":
        failures.append(f"stripped {stripped!r} no longer matches VolumeUp")

    # is_busy: an assistant turn in flight defers the idle timeout (a model
    # slower than holdWindowS must not be killed mid-answer), but a hung turn
    # expires after ASSISTANT_WAIT_S so it can't pin the session open.
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

    # Tier 2's way out: the stop_listening tool ARMS the gate, and the session
    # ends only once the goodbye has been spoken - the tool itself runs before
    # the model has said a word, so ending there would close the mic over it.
    import asyncio

    import cglib
    from pipecat.frames.frames import (BotStoppedSpeakingFrame, EndWorkerFrame,
                                       ErrorFrame)
    from pipecat.processors.frame_processor import FrameDirection

    def drive(frames, arm):
        """Feed frames to a fresh gate with push_frame stubbed (there is no
        pipeline here); return the EndWorkerFrames it pushed, and its log."""
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

    # An assistant turn is SILENT while it works: the only sounds around an
    # answer are the answer itself and, if it errors, the fail earcon.
    import earcons
    if "think" in earcons.SPECS:
        failures.append("the think earcon is back - it was removed on purpose")

    for f in failures:
        print("FAIL", f)
    assert not failures, f"{len(failures)} grammar failures"
    print(f"OK - {len(TABLE)} utterances: intents, slots, fall-throughs, "
          f"risky-command narrowness; {len(STRIP)} wake-strip cases; "
          f"is_busy defers for in-flight assistant turns; an armed stop ends "
          f"the session after the goodbye, never before")


if __name__ == "__main__":
    main()
