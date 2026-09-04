"""Handle fixed voice commands before sending other text to the assistant."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml
from hassil import Intents, SlotList, TextSlotList, recognize
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    ErrorFrame,
    Frame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from rapidfuzz import fuzz

from slopstation import events
from slopstation.agent.brain.dispatch import Result
from slopstation.agent.speech import earcons
from slopstation.agent.telemetry import sentry
from slopstation.agent.tools import titles

GRAMMAR = Path(__file__).resolve().parents[1] / "grammar.yaml"

GREETINGS = {"hey", "hi", "ok", "okay"}
_PUNCT = ",.!?"

# A joined pair is only a SPLIT anchor if its second half is a fragment; if
# that half already IS the anchor, joining would eat the real word in front
# ("a jarvis" 92.3, "my/is jarvis" 85.7, "the jarvis" exactly 80.0). A genuine
# split's second half only scores as a fragment ("fred" is 80 against
# "alfred"), so the bar sits above 80.
_WHOLE_ANCHOR = 90


def strip_wake(text: str, anchor: str = "jarvis") -> str:
    """Remove a leading wake phrase ("hey jarvis", "jarvis", mishears like
    "jervis") from a transcript; the pre-roll buffer includes it. Fuzzy on the
    anchor word only, >=80 (mishears like "jervis" ~83; real words like
    "travis" ~67); greeting optional; repeated, so a stuttered "hey jarvis hey
    jarvis volume up" still cleans up. Leading only - a mid-sentence "jarvis"
    is content.

    STT can split the anchor across two tokens ("hey alfred" -> "Hey, all.
    Fred,") and neither half clears 80 alone, so the two leading
    tokens are also tried JOINED - "allfred" ~92 against "alfred", non-wake
    pairs under the bar ("all for" ~67, "play jarvis" ~75), guarded by
    _WHOLE_ANCHOR."""
    while True:
        toks = text.split()
        i = 1 if (len(toks) > 1 and toks[0].strip(_PUNCT).lower() in GREETINGS) else 0
        if i < len(toks) and fuzz.ratio(toks[i].strip(_PUNCT).lower(), anchor) >= 80:
            text = " ".join(toks[i + 1 :])
            continue
        if (
            i + 1 < len(toks)
            and fuzz.ratio(toks[i + 1].strip(_PUNCT).lower(), anchor) < _WHOLE_ANCHOR
            and fuzz.ratio(
                (toks[i].strip(_PUNCT) + toks[i + 1].strip(_PUNCT)).lower(), anchor
            )
            >= 80
        ):
            text = " ".join(toks[i + 2 :])
            continue
        return text


def stt_confidence(frame) -> float | None:
    """Return mean per-word confidence, or None when unavailable."""
    try:
        words = (frame.result or {}).get("words") or []
        scores = [
            w["confidence"]
            for w in words
            if isinstance(w, dict) and w.get("confidence") is not None
        ]
        return round(sum(scores) / len(scores), 2) if scores else None
    except Exception:
        return None


def load_intents() -> Intents:
    return Intents.from_dict(yaml.safe_load(GRAMMAR.read_text(encoding="utf-8")))


class GrammarMatcher:
    """Pure logic (no pipecat) so tests and bench/probe_stt reuse it.
    Runtime slot lists: inputs from config, game titles from the library."""

    def __init__(self, voice_cfg: dict) -> None:
        # {game}/{collection} are wildcards - the fuzzy resolvers own those.
        # {input} and {target} are fixed runtime lists; {target}'s VALUE is
        # the nav kind (downloads/library/store), so no second mapping.
        self.intents = load_intents()
        self.slot_lists: dict[str, SlotList] = {
            "input": TextSlotList.from_tuples(
                (spoken, spoken) for spoken in voice_cfg["inputs"]
            ),
            "target": TextSlotList.from_tuples(
                (spoken, kind)
                for spoken, kind in voice_cfg.get("navTargets", {}).items()
            ),
        }

    def match(self, text: str) -> tuple | None:
        """(intent_name, slots dict) or None. Input goes through spoken_form
        so 'armored core six' meets the slot variant 'armored core 6'."""
        r = recognize(
            titles.spoken_form(text), self.intents, slot_lists=self.slot_lists
        )
        if r is None:
            return None
        return r.intent.name, {k: v.value for k, v in r.entities.items()}


class GrammarGate(FrameProcessor):
    """intent -> dispatch call; Result.earcon -> tone pushed to the speaker."""

    # Cap on an in-flight assistant turn before the idle handler stops
    # deferring. Covers think-before-speak (GPT at low effort) and a 15s ssh.
    ASSISTANT_WAIT_S = 30

    # Clear a user-speaking flag if Flux disconnects before sending its stop.
    SPEAKING_WAIT_S = 30

    # Suppress an immediate acknowledgement that would overlap the wake chime.
    ACK_COALESCE_S = 0.8

    def __init__(
        self,
        matcher,
        dispatch,
        log,
        resolve_game=None,
        assistant_enabled=False,
        wake_word=None,
        ack=None,
        resolve_collection=None,
    ):
        super().__init__()
        self.matcher = matcher
        self.dispatch = dispatch
        self.log = log
        self.resolve_game = resolve_game  # fuzzy title -> appid (titles.py)
        self.resolve_collection = resolve_collection  # fuzzy name -> collection id
        self.assistant_enabled = assistant_enabled
        self.wake_word = wake_word  # strip anchor ("jarvis"); None = off
        self.ack = ack  # preroll.WakeAck; None = no chime
        self._speaking = 0.0  # ts of the open user turn; 0 = closed
        self._dispatching = 0  # blocking calls in flight
        self._assistant_pending = 0.0  # ts of a transcript handed to the LLM
        self._stop_after_reply = False  # stop_listening tool armed one

    def request_stop(self) -> None:
        """End the session after the current assistant reply."""
        self._stop_after_reply = True
        self.log("session_stop_requested")

    async def _stop_if_armed(self, reason):
        """End the session if stop_listening armed one. Two different frames
        can prove the goodbye is over."""
        if not self._stop_after_reply:
            return
        self._stop_after_reply = False
        await self.push_frame(EndWorkerFrame(reason=reason))

    def is_busy(self) -> bool:
        """True while the user is mid-turn, a dispatch is running, or an
        assistant answer is in flight (cleared when the bot starts speaking).
        Without the in-flight check a model slower than holdWindowS is killed
        mid-answer. Both time flags expire, so a lost frame cannot defer the
        idle handler forever."""
        pending = (
            self._assistant_pending
            and time.time() - self._assistant_pending < self.ASSISTANT_WAIT_S
        )
        speaking = (
            self._speaking and time.time() - self._speaking < self.SPEAKING_WAIT_S
        )
        return bool(speaking) or self._dispatching > 0 or bool(pending)

    async def _earcon(self, name):
        await self.push_frame(
            OutputAudioRawFrame(
                audio=earcons.pcm(name), sample_rate=earcons.SAMPLE_RATE, num_channels=1
            )
        )

    async def _ack_wake(self):
        """The wake chime, unless the capture watcher already played it (it
        wins when you pause after "hey jarvis"; we win on a one-breath
        command). Lands when the turn ENDS. Once per session."""
        if self.ack is not None and self.ack.claim():
            await self._earcon("wake")

    async def _result_earcon(self, name):
        """Ack a dispatch result - unless it is a plain success still landing
        on the wake chime (see ACK_COALESCE_S). busy and fail always play."""
        if (
            name == "ok"
            and self.ack is not None
            and self.ack.age() < self.ACK_COALESCE_S
        ):
            self.log("earcon_folded", earcon=name)
            return
        await self._earcon(name)

    async def _run_intent(self, intent, slots):
        """True = consumed here; False = hand to the assistant lane after all
        (unresolvable title). Dispatch is blocking (ssh up to 15 s, serial) -
        run it off the event loop so audio and the Flux socket keep flowing."""
        d = self.dispatch
        if intent == "ExitSession":
            self.log("session_exit_phrase")
            # No earcon here: the sleep chime plays from the wake loop after
            # teardown, so every way a session can end sounds the same.
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
            "Nav": lambda: d.nav(str(slots["target"])),
        }
        self._dispatching += 1
        try:
            if intent == "PlayGame":
                r = await self._play_game(str(slots["game"]))
                if r is None:  # title unresolved -> the assistant
                    return False
            elif intent == "ShowCollection":
                r = await self._show_collection(str(slots["collection"]))
                if r is None:  # collection unresolved -> the assistant
                    return False
            elif intent in actions:
                r = await asyncio.to_thread(actions[intent])
            else:
                self.log.error("intent_unknown", intent=intent)
                return True
        finally:
            self._dispatching -= 1
        self.log("dispatch", intent=intent, ok=r.ok, detail=r.detail)
        await self._result_earcon(r.earcon)
        return True

    async def _play_game(self, spoken):
        """Resolve via titles.py; a miss goes to the assistant (it can reason
        about 'that mech game') or, without one, an honest fail earcon."""
        appid, title = self.resolve_game(spoken) if self.resolve_game else (None, None)
        if appid is None:
            if self.assistant_enabled:
                self.log("title_miss", spoken=spoken, fallback="assistant")
                return None
            self.log.warn(
                "title_miss",
                spoken=spoken,
                fallback="fail_earcon",
                reason=None if self.resolve_game else "no_index",
            )
            return Result(False, "fail", f"no match for '{spoken}'")
        self.log("title_resolved", spoken=spoken, title=title, appid=appid)
        return await asyncio.to_thread(self.dispatch.play_game, appid)

    async def _show_collection(self, spoken):
        """Resolve a collection name -> id via titles; a miss goes to the
        assistant (or a fail earcon without one), like a title miss."""
        cid, name = (
            self.resolve_collection(spoken) if self.resolve_collection else (None, None)
        )
        if cid is None:
            if self.assistant_enabled:
                self.log("collection_miss", spoken=spoken, fallback="assistant")
                return None
            self.log.warn(
                "collection_miss",
                spoken=spoken,
                fallback="fail_earcon",
                reason=None if self.resolve_collection else "no_collections",
            )
            return Result(False, "fail", f"no collection matching '{spoken}'")
        self.log("collection_resolved", spoken=spoken, name=name, id=cid)
        return await asyncio.to_thread(self.dispatch.nav, "collection", cid)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # The turn resolver emits these speaking frames.
        if isinstance(frame, UserStartedSpeakingFrame):
            self._speaking = time.time()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._speaking = 0.0
            await self._ack_wake()  # you stopped - chime now
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._assistant_pending = 0.0  # answer arrived; idle clock owns it now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Wait for the goodbye to finish before ending the session.
            await self._stop_if_armed("stop listening")
        elif isinstance(frame, ErrorFrame):
            # Copy Pipecat service errors into couch.log.
            self.log.error("pipeline_error", err=str(frame.error))
            if self._assistant_pending:
                self._assistant_pending = 0.0
                await self._earcon("fail")
            # Do not wait for a goodbye after the response failed.
            await self._stop_if_armed("stop listening (answer failed)")
        if (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            text = frame.text.strip()
            conf = stt_confidence(frame)
            if text:
                # Associate downstream events with this utterance.
                turn = events.new_turn()
                events.context(turn=turn)
                # Spans are exported off a batch thread that cannot see the
                # context above, so the turn is pinned for them separately.
                sentry.set_turn(turn)
                # Backstop: a final transcript proves the turn ended even if
                # no UserStoppedSpeakingFrame arrived (otherwise no feedback
                # until the action completes, up to 15 s of ssh).
                await self._ack_wake()
            if text and self.wake_word:
                stripped = strip_wake(text, self.wake_word)
                if not stripped:
                    # Pre-roll means a pause-style wake transcribes as just
                    # "hey jarvis": swallow it, no earcon, no LLM turn.
                    self.log(
                        "stt_final", text=text, outcome="wake_only", confidence=conf
                    )
                    return
                if stripped != text:
                    self.log("wake_prefix_stripped", text=text, stripped=stripped)
                    frame.text = stripped  # both lanes see the command only
                    text = stripped
            if text:
                # The utterance snapshot (see dispatch.Utterance). `asked` is
                # post-strip, so a queued job stores the command itself.
                self.dispatch.begin_utterance(turn, text)
                m = self.matcher.match(text)
                if m is not None:
                    self.log("gate_match", text=text, intent=m[0], confidence=conf)
                    if await self._run_intent(*m):
                        return  # swallowed: the gate handled it
                if not self.assistant_enabled:
                    self.log.warn(
                        "gate_miss",
                        text=text,
                        fallback="none",
                        reason="assistant_disabled",
                        confidence=conf,
                    )
                    await self._earcon("fail")
                    return
                # reason on BOTH gate_miss paths, so the field is total: one
                # that appears on only one path makes `reason:assistant_disabled`
                # look like the whole story.
                self.log(
                    "gate_miss",
                    text=text,
                    fallback="assistant",
                    reason="no_grammar_match",
                    confidence=conf,
                )
                self._assistant_pending = time.time()
        await self.push_frame(frame, direction)
