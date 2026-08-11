"""GrammarGate: Tier-1 deterministic intent matching as a Pipecat processor.

Sits between STT and everything else. Every FINAL transcript is screened here
FIRST - command matches are swallowed (the LLM lane never sees them), acked
with a count-coded earcon, and dispatched. Non-matches flow downstream to the
assistant lane (or dead-end with a fail earcon when no LLM key is configured).

Exit phrases end the session by pushing EndWorkerFrame downstream.
"""
import asyncio
from pathlib import Path

import yaml
from hassil import Intents, TextSlotList, recognize
from rapidfuzz import fuzz

from pipecat.frames.frames import (EndWorkerFrame, Frame, OutputAudioRawFrame,
                                   TranscriptionFrame, UserStartedSpeakingFrame,
                                   UserStoppedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import earcons
import titles

GRAMMAR = Path(__file__).resolve().parent / "grammar.yaml"

GREETINGS = {"hey", "hi", "ok", "okay"}
_PUNCT = ",.!?"


def strip_wake(text, anchor="jarvis"):
    """Remove a leading wake phrase ("hey jarvis", "jarvis", mishears like
    "jervis") from a transcript. The pre-roll buffer deliberately includes the
    wake phrase - Flux transcribes the whole utterance and text-side stripping
    is more reliable than trimming it out of the audio. Fuzzy on the anchor
    word only, >=80 (mishears like "jervis" score ~83; real words like
    "travis" ~67); greeting optional; repeated, so a stuttered "hey jarvis
    hey jarvis volume up" still cleans up. Leading only - a mid-sentence
    "jarvis" is content."""
    while True:
        toks = text.split()
        i = 1 if (len(toks) > 1 and toks[0].strip(_PUNCT).lower() in GREETINGS) else 0
        if i < len(toks) and fuzz.ratio(toks[i].strip(_PUNCT).lower(), anchor) >= 80:
            text = " ".join(toks[i + 1:])
            continue
        return text


def load_intents():
    return Intents.from_dict(yaml.safe_load(GRAMMAR.read_text(encoding="utf-8")))


class GrammarMatcher:
    """Pure logic (no pipecat) so tests and the --text REPL reuse it.
    Runtime slot lists: inputs from config, game titles from the library."""

    def __init__(self, voice_cfg):
        # {game} is a wildcard - the fuzzy resolver owns title matching.
        self.intents = load_intents()
        self.slot_lists = {
            "input": TextSlotList.from_tuples(
                (spoken, spoken) for spoken in voice_cfg["inputs"]),
        }

    def match(self, text):
        """Returns (intent_name, slots dict) or None. Input goes through
        spoken_form so 'armored core six' meets the slot variant
        'armored core 6' on equal terms."""
        r = recognize(titles.spoken_form(text), self.intents,
                      slot_lists=self.slot_lists)
        if r is None:
            return None
        return r.intent.name, {k: v.value for k, v in r.entities.items()}


class GrammarGate(FrameProcessor):
    """intent -> dispatch call; Result.earcon -> tone pushed to the speaker."""

    def __init__(self, matcher, dispatch, log, resolve_game=None,
                 assistant_enabled=False, wake_word=None):
        super().__init__()
        self.matcher = matcher
        self.dispatch = dispatch
        self.log = log
        self.resolve_game = resolve_game        # fuzzy title -> appid (titles.py)
        self.assistant_enabled = assistant_enabled
        self.wake_word = wake_word              # strip anchor ("jarvis"); None = off
        self._speaking = False                  # user turn open (Flux)
        self._dispatching = 0                   # blocking calls in flight

    def is_busy(self):
        """True while the user is mid-turn or a dispatch is running - the idle
        handler defers session-end until both are clear."""
        return self._speaking or self._dispatching > 0

    async def _earcon(self, name):
        await self.push_frame(OutputAudioRawFrame(
            audio=earcons.pcm(name), sample_rate=earcons.SAMPLE_RATE,
            num_channels=1))

    async def _run_intent(self, intent, slots):
        """Returns True if the utterance was consumed here (the usual case);
        False = hand it to the assistant lane after all (unresolvable title).
        Dispatch is blocking (ssh up to 15 s, serial) - run it off the event
        loop so audio and the Flux socket keep flowing while it works."""
        d = self.dispatch
        if intent == "ExitSession":
            self.log("exit phrase - ending voice session")
            await self._earcon("close")
            await self.push_frame(EndWorkerFrame(reason="exit phrase"))
            return True
        actions = {
            "StartSession": d.start_session,
            "EndSession": d.end_session,
            "VolumeUp": d.volume_up,
            "VolumeDown": d.volume_down,
            "VolumeSet": lambda: d.volume_set(int(slots["level"])),
            "MuteToggle": d.mute_toggle,
            "SwitchInput": lambda: d.switch_input(str(slots["input"])),
        }
        self._dispatching += 1
        try:
            if intent == "PlayGame":
                r = await self._play_game(str(slots["game"]))
                if r is None:                   # unresolvable title -> Tier 2
                    return False
            elif intent in actions:
                r = await asyncio.to_thread(actions[intent])
            else:
                self.log(f"grammar matched unknown intent {intent} - ignoring")
                return True
        finally:
            self._dispatching -= 1
        self.log(f"{intent} -> {r.detail}")
        await self._earcon(r.earcon)
        return True

    async def _play_game(self, spoken):
        """Resolve via titles.py; a miss goes to the assistant (it can reason
        about 'that mech game') or, without one, an honest fail earcon."""
        appid, title = (self.resolve_game(spoken) if self.resolve_game
                        else (None, None))
        if appid is None:
            if self.assistant_enabled:
                self.log(f"play '{spoken}' - no confident match, asking the assistant")
                return None
            self.log(f"play '{spoken}' - no confident title match"
                     + ("" if self.resolve_game else " (no library index yet)"))
            from dispatch import Result
            return Result(False, "fail", f"no match for '{spoken}'")
        self.log(f"play '{spoken}' -> {title} ({appid})")
        return await asyncio.to_thread(self.dispatch.play_game, appid)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._speaking = False
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = frame.text.strip()
            if text and self.wake_word:
                stripped = strip_wake(text, self.wake_word)
                if not stripped:
                    # Pre-roll means a pause-style wake transcribes as just
                    # "hey jarvis": not a command, not assistant material -
                    # swallow it and keep listening (no earcon, no LLM turn).
                    self.log(f'heard "{text}" - wake phrase only, listening')
                    return
                if stripped != text:
                    self.log(f'wake prefix stripped: "{text}" -> "{stripped}"')
                    frame.text = stripped       # both lanes see the command only
                    text = stripped
            if text:
                m = self.matcher.match(text)
                if m is not None:
                    self.log(f'heard "{text}" -> {m[0]}')
                    if await self._run_intent(*m):
                        return                      # swallowed: Tier 1 handled it
                if not self.assistant_enabled:
                    self.log(f'heard "{text}" - no command match '
                             f"(assistant lane not enabled)")
                    await self._earcon("fail")
                    return
                self.log(f'heard "{text}" - passing to assistant')
        await self.push_frame(frame, direction)
