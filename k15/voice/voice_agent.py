"""K15 voice agent: wake word -> session pipeline -> dispatch.

The wake loop runs OUTSIDE Pipecat (raw PyAudio + openWakeWord, zero cloud);
each wake builds and runs ONE PipelineWorker (mic -> Flux STT -> GrammarGate
-> speaker) that lives for the session and is torn down at its end. That
shape is forced by Flux: it connects on StartFrame with no app-facing
connect/disconnect and its socket dies ~20-30 s after audio stops, so a
per-session pipeline is also what makes idle cost $0. Sessions end on an exit
phrase or the idle timeout (holdWindowS).

This file is the COMPOSITION ROOT and the wake loop, nothing else: audio.py
owns the PortAudio world (devices, recovery, the wake listener) and
session_runtime.py owns one session's construction and lifecycle.

Modes:
  (default)             run the agent
  --devices             list audio devices and exit
  --earcons             play the earcon vocabulary and exit (volume audition)
  --announce-test       speak a canned job announcement and exit (audio path)
  --dry-run             full pipeline; side effects logged, not executed
  --wake-trials         log wake detections + confidences; never start sessions
  --false-accept-soak   count spurious wakes over hours; never start sessions
  --once                exactly one session, then exit (bench)

Overlay rule: this process is never load-bearing - the chord listener is a
separate process on system python and must survive anything that happens here.
"""
import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                    # noqa: E402
import earcons                                  # noqa: E402
import events                                   # noqa: E402
import library                                  # noqa: E402
import tracing                                  # noqa: E402
from audio import (WakeListener, list_devices, open_audio,  # noqa: E402
                   play_pcm, rebuild_audio)
from dispatch import TvDucker                   # noqa: E402
from grammar_gate import GrammarMatcher         # noqa: E402
from preroll import WakeAck                     # noqa: E402  (pipecat
# frames are already loaded via grammar_gate, so this adds no startup cost)
from session_runtime import run_session         # noqa: E402

log = cglib.make_log("voice")


def refresh_library_bg():
    """Full catalog sync off the wake loop - a slow/asleep PC (30 s ssh timeout)
    or a metadata crawl must never delay a wake. library.sync fail-softs, is
    key-gated per layer, and no-ops if one is already running."""
    threading.Thread(target=library.sync, daemon=True).start()


def prewarm_imports_bg(provider):
    """First-wake latency fix: pipecat's service modules + the provider SDK
    take several seconds to import on the K15's U-class CPU, which showed up
    as ~6.5 s of wake-to-listening dead air on the first session. Import
    them at boot on a background thread so the first session builds as fast
    as every later one (imports are idempotent and lock-protected)."""
    def warm():
        import pipecat.processors.aggregators.llm_response_universal  # noqa: F401
        import pipecat.services.deepgram.flux.stt   # noqa: F401
        import pipecat.services.deepgram.tts        # noqa: F401
        import pipecat.transports.local.audio       # noqa: F401
        if provider == "openai":
            import pipecat.services.openai.responses.llm  # noqa: F401
        else:
            import pipecat.services.anthropic.llm   # noqa: F401
    threading.Thread(target=warm, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--devices", action="store_true")
    ap.add_argument("--earcons", action="store_true",
                    help="play the earcon vocabulary through the configured "
                         "output device and exit (tune voice.earconGain by ear)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wake-trials", action="store_true")
    ap.add_argument("--false-accept-soak", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--announce-test", action="store_true",
                    help="speak a canned background-task announcement and "
                         "exit: the out-of-session audio path (earcon, Aura "
                         "synth, chunked playback) with no job, no quota")
    ap.add_argument("--text", action="store_true",
                    help="assistant REPL: typed transcripts, no audio; "
                         "always dry-run (actions log, never execute)")
    ap.add_argument("--provider", help="--text A/B: anthropic|openai")
    ap.add_argument("--model", help="--text A/B: model id override")
    ap.add_argument("--effort", help="--text A/B: openai reasoning effort "
                                     "(none|minimal|low|medium|high)")
    args = ap.parse_args()

    if args.devices:
        list_devices()
        return 0

    cfg = cglib.load_config()
    # The chord lane's keys too (dispatch reads tvComPort and tvGamingCmd)
    # and the voice section's - cglib's lists, the same ones doctor checks.
    missing = cglib.missing_config(cfg, voice=True)
    if missing:
        log.error("config_invalid", missing=missing)
        return 1
    voice = cfg["voice"]
    secrets = cglib.load_secrets()
    # Earcon volume is taste, and taste needs a knob you can turn from the
    # couch: optional (an already-deployed config must not fail to start).
    earcons.set_gain(voice.get("earconGain", 1.0))

    if args.earcons:
        pa, _, output_idx = open_audio(voice)
        log("earcon_audition", gain=earcons.GAIN)
        for name in earcons.SPECS:
            log("earcon_play", earcon=name)
            play_pcm(pa, earcons.pcm(name), output_idx)
            time.sleep(0.7)
        return 0

    if args.announce_test:
        import announce
        ann = announce.Announcer(voice, secrets, log)
        log("announce_test_start")
        try:
            done = ann.speak("Test announcement. This is how a finished "
                             "background task will reach you.")
        except Exception as e:
            log.error("announce_test_failed", err=str(e))
            return 1
        log("announce_test_done", complete=done)
        return 0

    if args.text:
        from assistant import repl
        return repl(cfg, secrets, log, dry_run=True, provider=args.provider,
                    model=args.model, effort=args.effort)

    cglib.rotate_log()
    # Waits for a configured mic that is still enumerating (a cold boot takes
    # ~15 s to get there) instead of starting deaf on the system default.
    pa, input_idx, output_idx = open_audio(voice)

    stt_live = cglib.real_key(secrets.get("deepgramApiKey"))
    if not stt_live:
        log.warn("lane_disabled", what="stt", reason="deepgram key is a placeholder")
    from assistant import PROVIDER_KEY
    brain_key = PROVIDER_KEY.get(voice["assistantProvider"])
    brain_live = bool(brain_key and cglib.real_key(secrets.get(brain_key)))
    # The two lanes take different value grammars for the same-looking key:
    # the assistant calls the Messages API (full ids only), the worker calls
    # the claude CLI (aliases fine, and preferable - they follow the latest).
    # Warn rather than refuse: a bad value for the INACTIVE provider must not
    # keep the agent down, it just has to stop being a silent trap for the
    # day someone flips assistantProvider.
    if not voice["assistantModelAnthropic"].startswith("claude-"):
        log.warn("config_suspect", setting="assistantModelAnthropic",
                 value=voice["assistantModelAnthropic"],
                 reason="not a full API model id (the assistant lane has no aliases)")
    if (voice["assistantWebSearch"]
            and voice["assistantProvider"] != "openai"):
        log.warn("config_suspect", setting="assistantWebSearch",
                 value=voice["assistantProvider"],
                 reason="production search runs on the openai lane only")

    # Build the grammar once (a YAML typo fails here, not per-wake); warm the
    # library index and the heavy pipeline imports in the background so the
    # first wake is as fast as every later one.
    matcher = GrammarMatcher(voice)
    refresh_library_bg()
    prewarm_imports_bg(voice["assistantProvider"])
    if brain_live:
        from assistant import default_model
        ap = voice["assistantProvider"]
        log("lane_up", what="assistant", provider=ap,
            model=default_model(cfg, ap),
            # anthropic has no effort knob
            effort=voice["assistantReasoningEffort"] if ap == "openai" else None,
            websearch=voice["assistantWebSearch"] or None)

    # Tier-3 worker lane, fail-soft like every other lane: a missing CLI turns
    # background tasks off with a clear message - wake, commands, and the
    # assistant are untouched either way.
    import announce
    import jobs as jobs_mod
    from workers import MODEL_KEY, WORKERS
    jobs = announcer = None
    wp = voice["workerProvider"]
    adapter = (WORKERS[wp](voice[MODEL_KEY[wp]], voice["workerEffort"])
               if wp in WORKERS else None)
    if adapter is None:
        log.warn("lane_disabled", what="worker", reason="unknown workerProvider",
                 provider=wp, known=list(WORKERS))
    elif not adapter.available():
        log.warn("lane_disabled", what="worker", reason="CLI not on PATH",
                 exe=adapter.exe)
    elif not (stt_live and brain_live):
        # The lane rides the assistant (only its background_task tool can
        # queue work) and Deepgram (announcements + retrieval TTS) - without
        # either it would be a store nothing fills and frames nothing speaks.
        log.warn("lane_disabled", what="worker",
                 reason="needs live Deepgram AND assistant keys")
    else:
        announcer = announce.Announcer(voice, secrets, log)
        jobs = jobs_mod.JobStore(log, adapter, voice["workerTimeoutS"],
                                 on_done=announcer.submit,
                                 dry_run=args.dry_run)
        announcer.jobs = jobs
        orphans = jobs.reconcile()
        jobs.start()
        # Spell out what IS running, not what config asked for: an empty
        # model means the CLI's own default.
        log("lane_up", what="worker", provider=wp, exe=adapter.exe,
            model=adapter.model or "(cli default)",
            effort=adapter.effort or "(cli default)", orphans=orphans or None)

    # Account session: install-by-voice + download status over ClientComm.
    # Self-gates on the refresh token exactly like the Steam key - no token,
    # the lane is None and install_game is never offered. Never fatal.
    import steam_session
    steam = steam_session.SteamSession(secrets, log,
                                       machine_name=cfg.get("steamMachineName"))
    if steam.available():
        exp = steam.token_expiry()
        log("lane_up", what="steam_session", steamid=steam.steamid,
            token_expires=(time.strftime("%Y-%m-%d", time.localtime(exp))
                           if exp else None))
    else:
        steam = None
        log("lane_disabled", what="steam_session",
            reason="no refresh token - run steam_session.py enroll")

    # Agent traces. Before the wake loop so the first session is traced like
    # every later one, and fail-soft: no keys, or a venv that predates the
    # OTel pins, disables the lane with a message and changes nothing else.
    tracing.setup(cfg, secrets, log)

    try:
        listener = WakeListener(pa, voice, input_idx)
    except FileNotFoundError as e:
        # _resolve_model already logged wake_model_missing with the paths it
        # tried; exiting cleanly keeps the supervisor's restart line readable
        # instead of burying it under a traceback every 10 s.
        print(f"[voice] {e}")
        return 1
    # model_source says WHICH copy answered - a vendored model and a
    # same-named pretrained one are indistinguishable from the name alone,
    # and that is exactly the skew worth catching from couch.log.
    log("agent_up", wake_model=listener.model_name,
        model_source=listener.model_source,
        threshold=voice["wakeThreshold"], dry_run=args.dry_run or None)
    # Liveness. The wake loop blocks in wait_for_wake_capture for minutes at a
    # time, so this has to be its own thread rather than a check in the loop.
    # Started only for a REAL run - the bench modes below exit, and a wake
    # trial or an --once session must not look like a live agent that then
    # went quiet (which would page).
    if not (args.wake_trials or args.false_accept_soak or args.once):
        events.start_heartbeat("voice")

    if args.wake_trials:
        log("wake_trials_start")
        n = 0
        while True:
            # peak, not score, is the number a threshold gets set from: score
            # is wherever the ramp happened to cross and clusters just above
            # the threshold whatever the model actually thought. Affordable
            # here because nothing follows the detection - see _scan_peak.
            score, peak = listener.wait_for_wake(
                voice["wakeThreshold"], peak_hops=WakeListener.PEAK_HOPS)
            n += 1
            log("wake_trial", n=n, score=round(score, 2), peak=round(peak, 3))
            play_pcm(pa, earcons.pcm("wake"), output_idx)
            time.sleep(1.0)                     # refractory: one hit per attempt

    if args.false_accept_soak:
        log("false_accept_soak_start")
        t0, n = time.time(), 0
        while True:
            # The peak matters even more on this side: it says how far ABOVE
            # the threshold the room can push the model, which is the margin
            # a real wake has to beat.
            _score, peak = listener.wait_for_wake(
                voice["wakeThreshold"], peak_hops=WakeListener.PEAK_HOPS)
            n += 1
            hrs = (time.time() - t0) / 3600
            log.warn("wake_false", n=n, hours=round(hrs, 2),
                     peak=round(peak, 3),
                     per_hour=round(n / max(hrs, 0.01), 1))
            time.sleep(1.0)

    # Room ducking for the length of a session (TvDucker has the whole
    # story: the 08-16 blind-burst incident behind the on-gate, and the
    # 08-21 eARC discovery behind keys-plus-readback - steps are SOUNDBAR
    # volume points, moved via remote-key relay and verified against the
    # TV's readback). OFF unless duckSteps is configured: this moves
    # something in the room, so it does not arrive switched on with a git
    # pull. It also needs tvIp - gate, keys and readback all live at that
    # address, and with nothing to ask it stays off rather than firing
    # blind. First use on a machine needs the one-time WS pairing:
    # `.venv\Scripts\python tv_remote.py pair`, accept on the TV.
    duck_steps = int(voice.get("duckSteps", 0) or 0)
    duck_to_pct = int(voice.get("duckToPct", 0) or 0)
    if duck_to_pct and not 0 < duck_to_pct < 100:
        log.warn("config_suspect", setting="duckToPct", value=duck_to_pct,
                 reason="duckToPct means duck TO that percent of the pre-duck "
                        "level, so only 1-99 makes sense - ignoring it")
        duck_to_pct = 0
    tv_ip = cfg.get("tvIp")
    if (duck_steps or duck_to_pct) and not tv_ip:
        log.warn("config_suspect", setting="duckSteps", value=duck_steps,
                 reason="ducking is configured but tvIp is not - it stays off "
                        "(gate, keys and readback all need the TV's address)")
    ducker = (TvDucker(duck_steps, tv_ip, log, dry_run=args.dry_run,
                       to_pct=duck_to_pct or None)
              if (duck_steps or duck_to_pct) and tv_ip else None)
    duck_lock = threading.Lock()

    def duck(restore):
        """Fire duck/unduck without making the session wait for the TV.

        Threaded because a burst costs real seconds - a ~1 s WebSocket
        connect, ~0.15 s per key, readback polls - and the session build is
        what the user is actually waiting on. The LOCK is what makes it
        correct rather than the thread: a session that ends quickly would
        otherwise start the unduck while the duck is still stepping, and
        the two would interleave into an arbitrary final volume. It also
        serializes the LEDGER - TvDucker is only ever touched under this
        lock.

        A hard process death between the two leaves the TV quiet - the ledger
        dies with the process and the supervisor's restart cannot know to
        undo it. Every ordinary ending, crash included, restores it from the
        session's finally, and a restore that fails THERE stays on the ledger
        as debt for the next session's close (tv_duck_deficit is the trace it
        leaves)."""
        if ducker is None:
            return

        def run():
            with duck_lock:
                try:
                    (ducker.unduck if restore else ducker.duck)()
                except Exception as e:
                    log.warn("tv_duck_failed", restore=restore, err=str(e))

        threading.Thread(target=run, daemon=True).start()

    while True:
        # The wake chime is armed, not played: whoever first hears the user
        # stop talking plays it - the capture watcher while the mic is still
        # ours (you paused after "hey jarvis"), or GrammarGate at end of turn
        # (one-breath command that outlasted the session build). Never over
        # the command itself, and it lands on the wait before the answer.
        ack = WakeAck()

        def chime_when_quiet(_ack=ack):
            if _ack.claim():
                play_pcm(pa, earcons.pcm("wake"), output_idx)

        try:
            score, capture = listener.wait_for_wake_capture(
                voice["wakeThreshold"], on_quiet=chime_when_quiet,
                interrupt=(announcer.follow_up.is_set if announcer else None))
        except OSError as e:
            # Mic stream death mid-listen (BT profile flap, device yanked,
            # AirPods multipoint wandering off) must never kill the agent -
            # voice is not load-bearing. Rebuild the PortAudio world, not
            # just the stream: reopening on the old instance is what went
            # deaf overnight (see rebuild_audio).
            log.error("wake_stream_died", err=str(e))
            pa, input_idx, output_idx = rebuild_audio(pa, voice, listener)
            continue
        if score is None:
            # A bulletin just finished playing: open the mic so the obvious
            # follow-up ("which one was cheapest?") needs no wake word. No
            # chime - the announcement WAS the cue, and the assistant already
            # has the result in context (job_messages).
            announcer.follow_up.clear()
            ack.claim()
            log("wake", trigger="follow_up")
        else:
            log("wake", trigger="wake_word", score=round(score, 2))
            if announcer:
                announcer.abort_current()       # user intent beats a bulletin
        if not stt_live:
            if capture:
                capture.stop()
            ack.claim()                         # no session: fail is the answer
            play_pcm(pa, earcons.pcm("fail"), output_idx)
            continue
        # One id per conversation, minted before the session's event loop
        # exists so asyncio.run carries it into every task inside (and
        # to_thread carries it into dispatch). Langfuse groups a wake plus its
        # follow-ups into one conversation on exactly this id at E5.
        events.context(session=events.new_turn())
        log("session_open")
        if announcer:
            announcer.session_active.set()
        ending = "close"
        try:
            # INSIDE the try so the unduck in the finally is unconditionally
            # paired with it; anything between the two that could raise would
            # otherwise leave the room quiet. It still lands before the
            # pipeline is up, which is what matters - the command arrives in
            # the seconds after this, and the 2 s pre-roll only covers what
            # was said BEFORE the wake word.
            duck(restore=False)
            asyncio.run(run_session(cfg, secrets, matcher, args,
                                    input_idx, output_idx, capture,
                                    jobs=jobs, ack=ack, steam=steam))
        except Exception as e:
            log.error("session_crashed", err=repr(e))
            ending = "fail"
        finally:
            if capture:                         # None on a follow-up open
                capture.stop()                  # idempotent; frees the mic if the build crashed
            if announcer:
                announcer.session_active.clear()
            duck(restore=True)                  # in the finally: a crashed
            # session must never be the reason the room stays quiet
        refresh_library_bg()                    # pick up installs between sessions
        # Going-to-sleep chime, after teardown so it marks the moment the mic
        # actually goes dormant - and every ending sounds the same, whether it
        # was an exit phrase, a stop_listening tool call, the idle timeout or a
        # crash (fail says which).
        play_pcm(pa, earcons.pcm(ending), output_idx)
        log("session_close", ending=ending)
        if args.once:
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("agent_stopped", reason="ctrl_c")
