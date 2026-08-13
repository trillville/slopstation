"""GrammarGate: Tier-1 deterministic intent matching as a Pipecat processor.

Sits between STT and everything else. Every FINAL transcript is screened here
FIRST - command matches are swallowed (the LLM lane never sees them), acked
with a count-coded earcon, and dispatched. Non-matches flow downstream to the
assistant lane (or dead-end with a fail earcon when no LLM key is configured).

Exit phrases end the session by pushing EndWorkerFrame downstream.
"""
import asyncio
import time
from pathlib import Path

import yaml
from hassil import Intents, TextSlotList, recognize
from rapidfuzz import fuzz

from pipecat.frames.frames import (BotStartedSpeakingFrame, EndWorkerFrame,
                                   ErrorFrame, Frame, OutputAudioRawFrame,
                                   TranscriptionFrame, TTSSpeakFrame,
                                   UserStartedSpeakingFrame,
                                   UserStoppedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import earcons
import events
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

    # How long an assistant turn may stay "in flight" before the idle handler
    # stops deferring for it. Covers a reasoning model's think-before-speak
    # (GPT at low effort) and a tool call's 15s ssh; caps so a hung or errored
    # turn can't pin the session open forever.
    ASSISTANT_WAIT_S = 30

    # A success earcon arriving while the wake chime is still ringing is one
    # sound too many - a local command dispatches in ~100 ms, so the two used
    # to run together. Fold it in: you just heard "got it", and nothing
    # further means it worked. Anything longer (ssh, a launch) clears the
    # window and acks normally, which is where "done" actually carries news.
    ACK_COALESCE_S = 0.8

    def __init__(self, matcher, dispatch, log, resolve_game=None,
                 assistant_enabled=False, wake_word=None, jobs=None,
                 ack=None):
        super().__init__()
        self.matcher = matcher
        self.dispatch = dispatch
        self.log = log
        self.resolve_game = resolve_game        # fuzzy title -> appid (titles.py)
        self.assistant_enabled = assistant_enabled
        self.wake_word = wake_word              # strip anchor ("jarvis"); None = off
        self.jobs = jobs                        # JobStore; None = worker lane off
        self.ack = ack                          # preroll.WakeAck; None = no chime
        self._speaking = False                  # user turn open (Flux)
        self._dispatching = 0                   # blocking calls in flight
        self._assistant_pending = 0.0           # ts of a transcript handed to the LLM

    def is_busy(self):
        """True while the user is mid-turn, a dispatch is running, or an
        assistant answer is still in flight (LLM reasoning + tool calls +
        TTS start, cleared when the bot starts speaking) - the idle handler
        defers session-end until all are clear. Without the in-flight check,
        a model slower than holdWindowS gets its session killed mid-answer."""
        pending = (self._assistant_pending
                   and time.time() - self._assistant_pending < self.ASSISTANT_WAIT_S)
        return self._speaking or self._dispatching > 0 or bool(pending)

    async def _earcon(self, name):
        await self.push_frame(OutputAudioRawFrame(
            audio=earcons.pcm(name), sample_rate=earcons.SAMPLE_RATE,
            num_channels=1))

    async def _ack_wake(self):
        """The wake chime, unless the capture watcher already played it while
        the mic was still ours (it wins when you pause after "hey jarvis"; we
        win on a one-breath command that outlasts the session build). Either
        way it lands when your turn ENDS - on your last word, not over it, and
        it fills the gap before the answer. Once per session: later turns get
        action earcons only."""
        if self.ack is not None and self.ack.claim():
            await self._earcon("wake")

    async def _result_earcon(self, name):
        """Ack a dispatch result - unless it is a plain success still landing
        on the wake chime (see ACK_COALESCE_S). busy and fail always play:
        they are news, and news is worth a second sound."""
        if (name == "ok" and self.ack is not None
                and self.ack.age() < self.ACK_COALESCE_S):
            self.log("earcon_folded", earcon=name)
            return
        await self._earcon(name)

    async def _run_intent(self, intent, slots):
        """Returns True if the utterance was consumed here (the usual case);
        False = hand it to the assistant lane after all (unresolvable title).
        Dispatch is blocking (ssh up to 15 s, serial) - run it off the event
        loop so audio and the Flux socket keep flowing while it works."""
        d = self.dispatch
        if intent == "ExitSession":
            self.log("session_exit_phrase")
            # No earcon here: the sleep chime now plays from the wake loop
            # after teardown, so every way a session can end - exit phrase,
            # idle, crash - sounds the same and marks the actual moment the
            # mic goes dormant.
            await self.push_frame(EndWorkerFrame(reason="exit phrase"))
            return True
        if intent in ("TaskResult", "TaskDetail", "TaskCancel"):
            if self.jobs is None:
                return False                    # worker lane off -> Tier 2
            return await self._task_intent(intent)
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
                self.log.error("intent_unknown", intent=intent)
                return True
        finally:
            self._dispatching -= 1
        self.log("dispatch", intent=intent, ok=r.ok, detail=r.detail)
        await self._result_earcon(r.earcon)
        return True

    async def _task_intent(self, intent):
        """Background-task retrieval speaks through the session TTS (the
        speech IS the feedback, no earcon); read only after it was spoken.
        All jobs calls are quick local file reads - no thread hop needed."""
        if intent == "TaskCancel":
            n, running = self.jobs.cancel_queued()
            self.log("task_cancel", cancelled=n, running=running)
            if running:
                await self.push_frame(TTSSpeakFrame(
                    "One is already running - it will finish or time out. "
                    + (f"Cancelled {n} queued." if n else "")))
            else:
                # Same fold rule as a dispatch ack: cancelling is a local file
                # read, so its ok would land inside the wake chime.
                await self._result_earcon("ok" if n else "fail")
            return True
        job = self.jobs.latest_result()
        if job is None:
            line = self.jobs.status_line()
            await self.push_frame(TTSSpeakFrame(
                line or "No finished background tasks."))
            return True
        text = job["detail"] if intent == "TaskDetail" else job["summary"]
        self.log("task_spoken", intent=intent, job=job["id"])
        await self.push_frame(TTSSpeakFrame(text))
        self.jobs.mark_read(job["id"])
        return True

    async def _play_game(self, spoken):
        """Resolve via titles.py; a miss goes to the assistant (it can reason
        about 'that mech game') or, without one, an honest fail earcon."""
        appid, title = (self.resolve_game(spoken) if self.resolve_game
                        else (None, None))
        if appid is None:
            if self.assistant_enabled:
                self.log("title_miss", spoken=spoken, fallback="assistant")
                return None
            self.log.warn("title_miss", spoken=spoken, fallback="fail_earcon",
                          reason=None if self.resolve_game else "no_index")
            from dispatch import Result
            return Result(False, "fail", f"no match for '{spoken}'")
        self.log("title_resolved", spoken=spoken, title=title, appid=appid)
        return await asyncio.to_thread(self.dispatch.play_game, appid)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._speaking = False
            await self._ack_wake()              # you stopped - chime now
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._assistant_pending = 0.0       # answer arrived; idle clock owns it now
        elif isinstance(frame, ErrorFrame):
            # Pipecat reports service failures (LLM 401/400, TTS death) via
            # loguru to the console only - mirror them into couch.log so a
            # silent assistant is diagnosable from the one log that matters.
            self.log.error("pipeline_error", err=str(frame.error))
            if self._assistant_pending:
                # The answer isn't coming: say so with the honest earcon
                # instead of trailing off into silence, and stop pinning the
                # idle handler open.
                self._assistant_pending = 0.0
                await self._earcon("fail")
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = frame.text.strip()
            if text:
                # A final transcript IS a user intent, so this is where the
                # turn id is born. Everything it causes - the gate decision,
                # the dispatch, couch.py, the gaming PC's Enter task - carries
                # it from here. The session id set at wake survives the merge.
                turn = events.new_turn()
                events.context(turn=turn)
                # Backstop: a final transcript proves the turn ended even if
                # no UserStoppedSpeakingFrame arrived. Silence here would mean
                # no feedback at all until the action completes (up to 15 s of
                # ssh), so never leave the chime to a single frame type.
                await self._ack_wake()
            if text and self.wake_word:
                stripped = strip_wake(text, self.wake_word)
                if not stripped:
                    # Pre-roll means a pause-style wake transcribes as just
                    # "hey jarvis": not a command, not assistant material -
                    # swallow it and keep listening (no earcon, no LLM turn).
                    self.log("stt_final", text=text, outcome="wake_only")
                    return
                if stripped != text:
                    self.log("wake_prefix_stripped", text=text, stripped=stripped)
                    frame.text = stripped       # both lanes see the command only
                    text = stripped
            if text:
                # THE utterance snapshot: the id minted above plus what the
                # user actually said, written to Dispatch as one immutable
                # pair. Explicit hand-over because a ContextVar cannot do it:
                # one is copied into a task at task CREATION, so setting it
                # here never reaches the assistant's tool dispatch - a
                # sibling task already running on an older snapshot. (The
                # session id works ambiently only because it is minted before
                # asyncio.run(); a per-utterance turn cannot be hoisted that
                # early.) When this relied on the ambient copy, voice-driven
                # exits reached the gaming PC uncorrelated and launches ran
                # under an id couch.py minted for itself. `asked` is captured
                # after wake-stripping, so it is the command and not "hey
                # jarvis, ..." - a queued job stores it beside the brief the
                # model writes, so a later replay quotes the user rather than
                # attributing the model's wording to them
                # (session_runtime.job_messages).
                if self.dispatch is not None:
                    self.dispatch.begin_utterance(turn, text)
                m = self.matcher.match(text)
                if m is not None:
                    self.log("gate_match", text=text, intent=m[0])
                    if await self._run_intent(*m):
                        return                      # swallowed: Tier 1 handled it
                if not self.assistant_enabled:
                    self.log.warn("gate_miss", text=text, fallback="none",
                                  reason="assistant_disabled")
                    await self._earcon("fail")
                    return
                self.log("gate_miss", text=text, fallback="assistant")
                self._assistant_pending = time.time()
        await self.push_frame(frame, direction)
