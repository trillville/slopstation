"""GrammarGate: Tier-1 deterministic intent matching as a Pipecat processor.

Sits between STT and everything else. Every FINAL transcript is screened here
FIRST - command matches are swallowed (the LLM lane never sees them), acked
with a count-coded earcon, and dispatched. Non-matches flow downstream (C3:
the assistant; until then they dead-end with a fail earcon).

Exit phrases end the session by pushing EndWorkerFrame downstream.
"""
import sys
from pathlib import Path

import yaml
from hassil import Intents, TextSlotList, recognize

from pipecat.frames.frames import (EndWorkerFrame, Frame, OutputAudioRawFrame,
                                   TranscriptionFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

sys.path.insert(0, str(Path(__file__).resolve().parent))
import earcons
import titles as titles_mod

GRAMMAR = Path(__file__).resolve().parent / "grammar.yaml"


def load_intents():
    return Intents.from_dict(yaml.safe_load(GRAMMAR.read_text(encoding="utf-8")))


class GrammarMatcher:
    """Pure logic (no pipecat) so tests and the C3 --text REPL reuse it.
    Runtime slot lists: inputs from config, game titles from the library."""

    def __init__(self, voice_cfg, titles=None):
        # titles arg kept for signature stability; {game} is a wildcard now -
        # the fuzzy resolver owns title matching (see grammar.yaml comment).
        self.intents = load_intents()
        self.slot_lists = {
            "input": TextSlotList.from_tuples(
                (spoken, spoken) for spoken in voice_cfg["inputs"]),
        }

    def match(self, text):
        """Returns (intent_name, slots dict) or None. Input goes through
        spoken_form so 'armored core six' meets the slot variant
        'armored core 6' on equal terms."""
        r = recognize(titles_mod.spoken_form(text), self.intents,
                      slot_lists=self.slot_lists)
        if r is None:
            return None
        return r.intent.name, {k: v.value for k, v in r.entities.items()}


class GrammarGate(FrameProcessor):
    """intent -> dispatch call; Result.earcon -> tone pushed to the speaker."""

    def __init__(self, matcher, dispatch, log, resolve_game=None,
                 assistant_enabled=False):
        super().__init__()
        self.matcher = matcher
        self.dispatch = dispatch
        self.log = log
        self.resolve_game = resolve_game        # C2: fuzzy title -> appid
        self.assistant_enabled = assistant_enabled

    async def _earcon(self, name):
        await self.push_frame(OutputAudioRawFrame(
            audio=earcons.pcm(name), sample_rate=earcons.SAMPLE_RATE,
            num_channels=1))

    async def _run_intent(self, intent, slots):
        """Returns True if the utterance was consumed here (the usual case);
        False = hand it to the assistant lane after all (unresolvable title)."""
        d = self.dispatch
        if intent == "ExitSession":
            self.log("exit phrase - ending voice session")
            await self._earcon("close")
            await self.push_frame(EndWorkerFrame(reason="exit phrase"))
            return True
        if intent == "StartSession":
            r = d.start_session()
        elif intent == "EndSession":
            r = d.end_session()
        elif intent == "VolumeUp":
            r = d.volume_up()
        elif intent == "VolumeDown":
            r = d.volume_down()
        elif intent == "VolumeSet":
            r = d.volume_set(int(slots["level"]))
        elif intent == "MuteToggle":
            r = d.mute_toggle()
        elif intent == "SwitchInput":
            r = d.switch_input(str(slots["input"]))
        elif intent == "PlayGame":
            r = self._play_game(str(slots["game"]))
            if r is None:                       # unresolvable title -> Tier 2
                return False
        else:
            self.log(f"grammar matched unknown intent {intent} - ignoring")
            return True
        self.log(f"{intent} -> {r.detail}")
        await self._earcon(r.earcon)
        return True

    def _play_game(self, spoken):
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
            return Result(False, "fail", f"I couldn't find {spoken}.",
                          f"no match for '{spoken}'")
        self.log(f"play '{spoken}' -> {title} ({appid})")
        return self.dispatch.play_game(appid)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = frame.text.strip()
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
