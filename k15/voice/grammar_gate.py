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

GRAMMAR = Path(__file__).resolve().parent / "grammar.yaml"


def load_intents():
    return Intents.from_dict(yaml.safe_load(GRAMMAR.read_text(encoding="utf-8")))


class GrammarMatcher:
    """Pure logic (no pipecat) so tests and the C3 --text REPL reuse it.
    Runtime slot lists: inputs from config, game titles from the library."""

    def __init__(self, voice_cfg, titles=None):
        self.intents = load_intents()
        self.slot_lists = {
            "input": TextSlotList.from_tuples(
                (spoken, spoken) for spoken in voice_cfg["inputs"]),
            "game": TextSlotList.from_tuples(
                (t, t) for t in (titles or ["__no_games_indexed__"])),
        }

    def match(self, text):
        """Returns (intent_name, slots dict) or None."""
        r = recognize(text.strip().lower(), self.intents,
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
        d = self.dispatch
        if intent == "ExitSession":
            self.log("exit phrase - ending voice session")
            await self._earcon("close")
            await self.push_frame(EndWorkerFrame(reason="exit phrase"))
            return
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
        else:
            self.log(f"grammar matched unknown intent {intent} - ignoring")
            return
        self.log(f"{intent} -> {r.detail}")
        await self._earcon(r.earcon)

    def _play_game(self, spoken):
        if self.resolve_game is None:
            return self.dispatch.start_session()   # no library yet: plain start
        appid, title = self.resolve_game(spoken)
        if appid is None:
            self.log(f"play '{spoken}' - no confident title match")
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
                    await self._run_intent(*m)
                    return                          # swallowed: Tier 1 handled it
                if not self.assistant_enabled:
                    self.log(f'heard "{text}" - no command match '
                             f"(assistant lane not enabled)")
                    await self._earcon("fail")
                    return
                self.log(f'heard "{text}" - passing to assistant')
        await self.push_frame(frame, direction)
