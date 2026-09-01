"""K15 voice agent: wake word -> session pipeline -> dispatch.

Each wake builds ONE PipelineWorker (mic -> Flux STT -> turn resolver ->
GrammarGate -> speaker), torn down at session end; the wake loop itself is
raw PyAudio + openWakeWord, outside Pipecat. Forced by Flux: it connects when the pipeline
comes up (during setup since pipecat 1.8) with no app-facing
connect/disconnect, and its socket dies ~20-30 s after audio stops. Sessions
end on an exit phrase or the idle timeout (holdWindowS).

Modes:
  (default)             run the agent
  --devices             list audio devices and exit
  --earcons             play the earcon vocabulary and exit (volume audition)
  --announce-test       speak a canned operation announcement and exit
  --dry-run             full pipeline; side effects logged, not executed
  --wake-trials         log wake detections + confidences; never start sessions
  --false-accept-soak   count spurious wakes over hours; never start sessions
  --once                exactly one session, then exit (bench)
  --text                the assistant REPL (brain/backends.py); --provider,
                        --model, --effort pick the A/B side

Composition root and wake loop only; audio.py owns PortAudio,
session_runtime.py owns one session. Never load-bearing: the chord listener is
a separate process and must survive anything that happens here.
"""
import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cglib
from agent.speech import earcons
import events
from agent.tools import library
from agent.telemetry import tracing
from agent.speech.audio import (WakeListener, list_devices, open_audio,
                                play_pcm, rebuild_audio)
from agent.speech.grammar_gate import GrammarMatcher
from agent.speech.preroll import WakeAck
from agent.speech.session_runtime import run_session
from agent.tools.tv_remote import TvDucker

log = cglib.make_log("voice")

def refresh_library_bg():
    """Catalog sync off the wake loop: a slow/asleep PC (30 s ssh timeout)
    must not delay a wake. Fail-soft; no-ops if one is running."""
    threading.Thread(target=library.sync, daemon=True).start()


def prewarm_imports_bg(provider):
    """Import pipecat's services + the provider SDK at boot: several seconds
    on the K15's U-class CPU, once ~6.5 s of dead air on the first wake.
    Safe off-thread - imports are idempotent and lock-protected."""
    def warm():
        import pipecat.pipeline.pipeline            # noqa: F401
        import pipecat.pipeline.worker              # noqa: F401
        import pipecat.processors.aggregators.llm_response_universal  # noqa: F401
        import pipecat.services.deepgram.flux.stt   # noqa: F401
        import pipecat.services.deepgram.tts        # noqa: F401
        import pipecat.transports.local.audio       # noqa: F401
        import pipecat.turns.user_turn_processor    # noqa: F401
        import pipecat.workers.runner               # noqa: F401
        if provider == "openai":
            import pipecat.services.openai.responses.llm  # noqa: F401
        else:
            import pipecat.services.anthropic.llm   # noqa: F401
        # Last and guarded: pipecat 1.8 defers nltk (+sklearn) to a warm that
        # otherwise runs INSIDE the first worker's setup - StartFrame waits on
        # it (~0.9 s dev box), and a missing punkt_tab even runs nltk.download
        # there against the 20 s setup ceiling. Internal module, so a pipecat
        # rename must not cost the imports above.
        try:
            from pipecat.utils.prewarm import warm_deferred_imports
            warm_deferred_imports()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()


def bench_mode(args, cfg, secrets):
    """The one-shot modes that need config but no wake loop: the exit code,
    or None to run the agent. --devices is handled before config."""
    voice = cfg["voice"]
    if args.earcons:
        pa, _, output_idx = open_audio(voice)
        log("earcon_audition", gain=earcons.GAIN)
        for name in earcons.SPECS:
            log("earcon_play", earcon=name)
            play_pcm(pa, earcons.pcm(name), output_idx)
            time.sleep(0.7)
        return 0

    if args.announce_test:
        from agent.speech import announce
        ann = announce.Announcer(voice, secrets, log)
        log("announce_test_start")
        try:
            done = ann.speak("Test announcement. This is how a finished "
                             "operation will reach you.")
        except Exception as e:
            log.error("announce_test_failed", err=str(e))
            return 1
        log("announce_test_done", complete=done)
        return 0

    if args.text:
        from agent.brain.backends import repl
        return repl(cfg, secrets, log, dry_run=True, provider=args.provider,
                    model=args.model, effort=args.effort)
    return None


def warn_config(voice):
    """Settings that are legal but probably not what was meant. Warn, never
    refuse: the INACTIVE provider must not block startup."""
    # The Messages API accepts full model ids, not CLI aliases.
    if not voice["assistantModelAnthropic"].startswith("claude-"):
        log.warn("config_suspect", setting="assistantModelAnthropic",
                 value=voice["assistantModelAnthropic"],
                 reason="not a full API model id (the assistant lane has no aliases)")
    if (voice["assistantWebSearch"]
            and voice["assistantProvider"] != "openai"):
        log.warn("config_suspect", setting="assistantWebSearch",
                 value=voice["assistantProvider"],
                 reason="production search runs on the openai lane only")


def make_ducker(cfg, dry_run):
    """The session ducker as one duck(restore) call; a no-op when ducking is
    off."""
    voice = cfg["voice"]
    # Session-length room ducking; details on TvDucker. Steps are SOUNDBAR
    # volume points, moved by remote-key relay and verified against the TV's
    # readback. Off unless duckSteps/duckToPct AND tvIp are set. First use on
    # a machine needs `.venv\Scripts\python tv_remote.py pair`, accepted on
    # the TV.
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
    ducker = (TvDucker(duck_steps, tv_ip, log, dry_run=dry_run,
                       to_pct=duck_to_pct or None)
              if (duck_steps or duck_to_pct) and tv_ip else None)
    duck_lock = threading.Lock()

    def duck(restore):
        """Fire duck/unduck without making the session wait for the TV.

        Threaded because a burst costs real seconds (~1 s WebSocket connect,
        ~0.15 s per key, plus readback polls). The LOCK is what makes it
        correct: a quick session would otherwise start the unduck mid-duck and
        the two would interleave into an arbitrary final volume. It also
        serializes the ledger - TvDucker is only touched under this lock. A
        failed restore stays on the ledger as debt for the next session's
        close (tv_duck_deficit); a hard process death loses it."""
        if ducker is None:
            return

        def run():
            with duck_lock:
                try:
                    (ducker.unduck if restore else ducker.duck)()
                except Exception as e:
                    log.warn("tv_duck_failed", restore=restore, err=str(e))

        threading.Thread(target=run, daemon=True).start()

    return duck


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
                    help="speak a canned operation announcement and "
                         "exit: the out-of-session audio path (earcon, Aura "
                         "synth, chunked playback) with no operation")
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

    cfg = cglib.config()
    voice = cfg["voice"]
    missing = cglib.missing_config(cfg, voice=True)
    if missing:
        log.error("config_invalid", missing=missing)
        return 1
    secrets = cglib.load_secrets()
    earcons.set_gain(voice.get("earconGain", 1.0))

    rc = bench_mode(args, cfg, secrets)
    if rc is not None:
        return rc

    cglib.rotate_log()
    stt_live = cglib.real_key(secrets.get("deepgramApiKey"))
    if not stt_live:
        log.warn("lane_disabled", what="stt", reason="deepgram key is a placeholder")
    from agent.brain.assistant import PROVIDER_KEY
    brain_key = PROVIDER_KEY.get(voice["assistantProvider"])
    brain_live = bool(brain_key and cglib.real_key(secrets.get(brain_key)))
    warn_config(voice)

    # Grammar built once: a YAML typo fails here, not per-wake.
    matcher = GrammarMatcher(voice)
    refresh_library_bg()
    prewarm_imports_bg(voice["assistantProvider"])
    if brain_live:
        from agent.brain.assistant import default_model
        provider = voice["assistantProvider"]
        log("lane_up", what="assistant", provider=provider,
            model=default_model(voice, provider),
            # anthropic has no effort knob
            effort=voice["assistantReasoningEffort"] if provider == "openai" else None,
            websearch=voice["assistantWebSearch"] or None)

    # Durable external operations and their out-of-session delivery.
    from agent.speech import announce
    from agent.tools import operations as operations_mod
    from agent.tools import operations_monitors
    operation_store = operations_mod.OperationStore(log)
    announcer = None
    if stt_live and not args.dry_run:
        announcer = announce.Announcer(voice, secrets, log)
        announcer.store = operation_store
        operation_store.on_terminal = announcer.submit
        operation_store.on_notification = announcer.submit_notification
        for operation in operation_store.pending_announcements():
            announcer.submit(operation)
        for notification in operation_store.pending_notifications():
            announcer.submit_notification(notification)

    # Remote install + download status over ClientComm. Without a refresh token,
    # install_game keeps its controller-driven fallback. Never fatal.
    from agent.tools import steam_session
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

    if steam is not None and not args.dry_run:
        monitor = operations_monitors.SteamMonitor(operation_store, steam, log)
        monitor.start()
        log("lane_up", what="operation_monitor",
            active=len(operation_store.active(kind="steam_install")),
            poll_s=monitor.poll_s)

    from agent.tools import media as media_mod
    media_service = media_mod.from_config(cfg, secrets, log)
    # Its reconcile dispatches deferred Sonarr searches and indexer-recovery
    # retries, both of which POST to the authority.
    if media_service is not None and not args.dry_run:
        poll_s = cfg["media"].get("pollS", operations_mod.POLL_S)
        media_monitor = operations_monitors.MediaMonitor(
            operation_store, media_service, log, poll_s=poll_s)
        media_monitor.start()
        active_media = sum(len(operation_store.active(kind=kind)) for kind in
                           media_monitor.KINDS)
        log("lane_up", what="media_operation_monitor", active=active_media,
            poll_s=media_monitor.poll_s)

    proton_port_monitor = media_mod.proton_port_monitor_from_config(
        cfg, secrets, log)
    # It writes the listening port into a live qBittorrent, so a dry run must
    # not start it.
    if proton_port_monitor is not None and not args.dry_run:
        proton_port_monitor.start()
        log("lane_up", what="proton_port_sync",
            poll_s=proton_port_monitor.poll_s)

    media_health_monitor = media_mod.media_health_monitor_from_config(
        cfg, secrets, log)
    if media_health_monitor is not None:
        media_health_monitor.start()
        log("lane_up", what="media_health_sync",
            poll_s=media_health_monitor.poll_s)

    disk_health_monitor = media_mod.disk_health_monitor_from_config(cfg, log)
    if disk_health_monitor is not None:
        disk_health_monitor.start()
        log("lane_up", what="disk_watch", poll_s=disk_health_monitor.poll_s,
            mounts=" ".join(disk_health_monitor.mounts))

    from agent.interfaces import text
    text.start(cfg, secrets, log, operations=operation_store,
                         steam=steam, media=media_service,
                         dry_run=args.dry_run)

    # Forwards to the text interface over localhost, so it takes no tools and
    # no dry_run of its own - both ride along inside that hop.
    from agent.interfaces import remote
    remote.start(cfg, secrets, log)

    # Before the wake loop, so the first session is traced too. Fail-soft.
    tracing.setup(cfg, secrets, log)

    # LAST: open_audio blocks until the configured device answers (~15 s on a
    # cold boot; forever on a dead mic). Everything above must already be
    # serving - text, MCP, and the monitors must not wait on a microphone.
    pa, input_idx, output_idx = open_audio(voice)

    try:
        listener = WakeListener(pa, voice, input_idx)
    except FileNotFoundError as e:
        # wake_model_missing is already logged with the paths tried.
        print(f"[voice] {e}")
        return 1
    # model_source: a vendored and a pretrained model can share a name.
    log("agent_up", wake_model=listener.model_name,
        model_source=listener.model_source,
        threshold=voice["wakeThreshold"], dry_run=args.dry_run or None)
    # Own thread: the wake loop blocks for minutes. Real runs only - a quiet
    # bench mode would page.
    if not (args.wake_trials or args.false_accept_soak or args.once):
        events.start_heartbeat("voice")

    if args.wake_trials:
        log("wake_trials_start")
        n = 0
        while True:
            # Thresholds are set from the peak, not the crossing score.
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
            # Peak = the margin above threshold a real wake has to beat.
            _score, peak = listener.wait_for_wake(
                voice["wakeThreshold"], peak_hops=WakeListener.PEAK_HOPS)
            n += 1
            hrs = (time.time() - t0) / 3600
            log.warn("wake_false", n=n, hours=round(hrs, 2),
                     peak=round(peak, 3),
                     per_hour=round(n / max(hrs, 0.01), 1))
            time.sleep(1.0)

    duck = make_ducker(cfg, args.dry_run)

    while True:
        # The chime is armed, not played: whoever first hears the user stop
        # talking plays it (capture watcher, or GrammarGate at end of turn),
        # so it never lands over the command.
        ack = WakeAck()

        def chime_when_quiet(_ack=ack):
            if _ack.claim():
                play_pcm(pa, earcons.pcm("wake"), output_idx)

        try:
            score, capture = listener.wait_for_wake_capture(
                voice["wakeThreshold"], on_quiet=chime_when_quiet,
                interrupt=(announcer.follow_up.is_set if announcer else None))
        except OSError as e:
            # Mic stream death mid-listen (BT profile flap, device yanked)
            # must never kill the agent. Rebuild the whole PortAudio world.
            log.error("wake_stream_died", err=str(e))
            pa, input_idx, output_idx = rebuild_audio(pa, voice, listener)
            continue
        # One id per conversation, minted before the event loop exists so
        # asyncio.run carries it into every task inside (and to_thread into
        # dispatch). Langfuse groups follow-ups on it. Minted after the wait
        # returns (a wake_stream_died must not carry a session that never
        # opens) and before log("wake"), so the wake carries the session it
        # opens.
        events.context(session=events.new_turn())
        if score is None:
            # A bulletin just finished: the mic opens for a follow-up with no
            # wake word, and no chime - the announcement was the cue.
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
            ack.claim()
            play_pcm(pa, earcons.pcm("fail"), output_idx)
            continue
        log("session_open")
        if announcer:
            announcer.session_active.set()
        ending = "close"
        try:
            # Inside the try so the finally's unduck is always paired with it.
            duck(restore=False)
            asyncio.run(run_session(cfg, secrets, matcher, args.dry_run,
                                    input_idx, output_idx, capture,
                                    operations=operation_store, ack=ack,
                                    steam=steam, media=media_service,
                                    on_end_session=lambda: duck(restore=True)))
        except Exception as e:
            log.error("session_crashed", err=repr(e))
            ending = "fail"
        finally:
            if capture:                         # None on a follow-up open
                capture.stop()                  # idempotent; frees the mic if the build crashed
            if announcer:
                announcer.session_active.clear()
            duck(restore=True)                  # a crash must not leave the room quiet
        refresh_library_bg()                    # pick up installs between sessions
        # Sleep chime after teardown: it marks the mic actually going dormant.
        play_pcm(pa, earcons.pcm(ending), output_idx)
        log("session_close", ending=ending)
        if args.once:
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("agent_stopped", reason="ctrl_c")
