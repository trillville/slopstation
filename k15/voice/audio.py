"""One PortAudio world: device resolution, recovery, out-of-session playback,
and the wake listener. Its own module because every deafness incident lives
here, and so does the invariant they all taught: NEVER resolve a device
against a PyAudio instance that predates a device change - PortAudio
snapshots the device table at init.

voice_agent.py composes these; announce.py resolves its per-announcement
output here rather than keeping its own copy of the resolver.
"""
import collections
import time
import wave
from pathlib import Path

import cglib
import earcons
from preroll import WakeCapture

log = cglib.make_log("voice")

RETRY_S = 5                                     # device-wait poll interval
WAIT_QUIET_S = 30                               # re-log an ongoing wait this often

# Fired pre-roll, kept on disk. Under logs/ so .gitignore already covers it and
# it prunes from the same folder a human already knows to open.
CLIPS_DIR = cglib.BASE / "logs" / "wake"


class DeviceMissing(Exception):
    """A device name-fragment IS configured but nothing matches it right now.
    Distinct from "no fragment configured", which legitimately means the
    system default - conflating those two is what went deaf for 5 minutes
    (the incident is on open_audio)."""

    def __init__(self, kind, wanted):
        super().__init__(f"no {kind} device matching {wanted!r}")
        self.kind = kind
        self.wanted = wanted


def resolve_device(pa, fragment, want_input, log=log, required=True):
    """Config name-fragment -> PyAudio device index; None = system default.
    Logs the bound NAME: after a rebuild the index alone says nothing about
    which physical mic/speaker is live, and the name is the only way to spot
    a wrong endpoint from couch.log. log=None resolves silently (the
    announcer re-resolves per bulletin - a line each would be noise).

    None means system default and NOTHING ELSE: a configured device that is
    absent raises DeviceMissing instead of quietly becoming the default,
    because the two need opposite responses - one is the config working as
    written, the other is "it is coming back, wait". required=False keeps the
    lenient answer for the one caller that wants it (announce.py)."""
    kind = "input" if want_input else "output"
    if not fragment:
        if log:
            log("audio_device", kind=kind, device="system default")
        return None
    frag = fragment.lower()
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        channels = d["maxInputChannels"] if want_input else d["maxOutputChannels"]
        if channels and frag in d["name"].lower():
            if log:
                log("audio_device", kind=kind, device=d["name"], index=i)
            return i
    if log:
        log.warn("audio_device_missing", kind=kind, wanted=fragment)
    if required:
        raise DeviceMissing(kind, fragment)
    return None


def list_devices():
    """Raw PortAudio view for --devices: every endpoint with its channel
    counts, and which two are the system defaults. Set config's
    inputDeviceName/outputDeviceName to a unique fragment of the name you
    want; leave them empty to take the defaults."""
    import pyaudio
    pa = pyaudio.PyAudio()
    print("[devices] host APIs: "
          + ", ".join(pa.get_host_api_info_by_index(i)["name"]
                      for i in range(pa.get_host_api_count())))

    def default_index(getter, what):
        try:
            return getter()["index"]
        except OSError:
            print(f"[devices] WARNING: no default {what} device")
            return None

    idx_in = default_index(pa.get_default_input_device_info, "input")
    idx_out = default_index(pa.get_default_output_device_info, "output")
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        tags = []
        if d["maxInputChannels"]:
            tags.append(f"in:{d['maxInputChannels']}")
        if d["maxOutputChannels"]:
            tags.append(f"out:{d['maxOutputChannels']}")
        mark = ""
        if i == idx_in:
            mark += " <= default input"
        if i == idx_out:
            mark += " <= default output"
        print(f"[devices] {i:3d} {d['name']} ({', '.join(tags)}){mark}")
    pa.terminate()


def build_audio(voice):
    """One PortAudio world: a fresh instance plus both devices resolved by
    name. PortAudio snapshots the device table at init, so a fresh instance
    is the ONLY way to see endpoints that (re)appeared since the last one -
    never resolve against a pa that predates a device change.

    Raises DeviceMissing if a configured device is not in the table; callers
    that must not run on the wrong endpoint go through open_audio instead.
    The instance is torn down before that raise escapes - open_audio retries
    every RETRY_S, so a leak here would strand a host handle per round for as
    long as the outage lasts."""
    import pyaudio
    pa = pyaudio.PyAudio()
    try:
        return (pa,
                resolve_device(pa, voice["inputDeviceName"], want_input=True),
                resolve_device(pa, voice["outputDeviceName"], want_input=False))
    except Exception:
        try:
            pa.terminate()
        except Exception as e:
            log.warn("audio_teardown_failed", err=str(e))
        raise


def open_audio(voice, log=log):
    """build_audio, but WAITING for a configured device that isn't there yet
    rather than settling for whatever Windows calls the default.

    The wait is the whole point. resolve_device used to answer "not found"
    with None, which build_audio passed on as "system default" and the caller
    accepted as success - so a USB array that dropped off mid-evening was
    replaced, silently, by an endpoint nobody chose. The wake loop then opened
    THAT, got -9999, and asked for another rebuild: 62 rounds at 5 s each,
    5 min 10 s deaf, every round logging a recovery that had not happened.
    Same policy for startup and for recovery, because the situation is the
    same one - the device is not here yet - and a single home for it is how
    the startup path stops being the lenient one.

    Loops until the real device answers: voice is not load-bearing, and
    dormant-but-alive beats listening to the wrong room."""
    waited = 0.0
    while True:
        try:
            return build_audio(voice)
        except DeviceMissing as e:
            # First miss and then every WAIT_QUIET_S: an outage is one event
            # with a duration on it, not a scroll of identical pairs. The
            # 5-minute deafness was invisible because it read as normal churn.
            if waited % WAIT_QUIET_S < RETRY_S:
                log.error("audio_device_wait", kind=e.kind, wanted=e.wanted,
                          waited_s=round(waited), retry_s=RETRY_S)
        except Exception as e:
            log.error("audio_rebuild_failed", err=str(e), retry_s=RETRY_S)
        time.sleep(RETRY_S)
        waited += RETRY_S


def rebuild_audio(old_pa, voice, listener):
    """Recovery from a dead wake stream: tear the whole PortAudio instance
    down and rebuild against the current device table. Reopening on the old
    instance retries a stale index - a reconnected headset gets a NEW index
    the old snapshot can't see - so the reopen either fails forever or
    'succeeds' onto a dead endpoint and goes deaf - observed as 240 blind
    reopens over 2.6 h, then a zombie stream, deaf until morning."""
    try:
        old_pa.terminate()
    except Exception as e:
        log.warn("audio_teardown_failed", err=str(e))
    time.sleep(RETRY_S)                         # let the endpoint settle first
    pa, input_idx, output_idx = open_audio(voice)
    listener.rebind(pa, input_idx)
    return pa, input_idx, output_idx


def play_pcm(pa, pcm, device_index=None):
    """Blocking playback for the earcons that fire outside a session: the
    wake chime (the pipeline isn't up yet) and the sleep chime (it is already
    torn down). One retry after a settle: Bluetooth outputs (AirPods)
    renegotiate profiles around our stream churn and can transiently refuse
    to open (-9999). A missed chime must never take the agent down either."""
    import pyaudio
    for attempt in (1, 2):
        try:
            s = pa.open(format=pyaudio.paInt16, channels=1,
                        rate=earcons.SAMPLE_RATE, output=True,
                        output_device_index=device_index)
            try:
                s.write(pcm)
            finally:
                s.stop_stream()
                s.close()
            return
        except OSError as e:
            if attempt == 1:
                time.sleep(0.5)
            else:
                log.warn("earcon_failed", err=str(e))


def dump_clip(ring, score, keep):
    """Write the pre-roll that fired to a wav, oldest pruned past `keep`.

    The corpus is the point. openWakeWord's custom verifier takes real
    false activations as its negative examples, and on 2026-08-15 the only
    record of three of them was the transcript the STT happened to make of
    whatever the TV said next - the audio itself was gone. Nothing can be
    re-scored, re-thresholded or trained against a transcript.

    This is the PRE-detection window, so replaying it reproduces the score up
    to the crossing and NOT the peak, which comes from audio after it (see
    _scan_peak). Local only, never uploaded - README's "Deliberately not
    doing" closes audio upload and this does not reopen it.

    Fail-soft to the point of silence: a full disk must cost a log line, not
    the session that is already building behind this call."""
    if keep <= 0 or not ring:
        return
    try:
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        # Milliseconds are load-bearing, not decoration: pruning below trusts
        # names to sort chronologically, and two fires inside one second (a
        # retry burst, a stuttered phrase) would otherwise collide on the name
        # and silently overwrite. Taken from the same `now` as the seconds
        # field so the tiebreaker cannot wrap past its own second.
        now = time.time()
        name = (f"wake-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"
                f"-{int(now * 1000) % 1000:03d}-{score:.3f}.wav")
        with wave.open(str(CLIPS_DIR / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"".join(ring))
        # Timestamped names sort chronologically, so the oldest are the head
        # of the sorted list and no stat() per file is needed to find them.
        for old in sorted(CLIPS_DIR.glob("wake-*.wav"))[:-keep]:
            old.unlink(missing_ok=True)
        log("wake_clip", clip=name, secs=round(len(ring) * 0.08, 1))
    except Exception as e:
        log.warn("wake_clip_failed", err=str(e))


def close_stream_quietly(stream):
    """Best-effort close for a possibly-dead stream: after a -9999 host error
    (BT profile flap, device yanked) the stream is already torn down and
    stop/close themselves raise 'Stream not open' - the original error is the
    story, and cleanup replacing it is what crashed the agent."""
    for op in (stream.stop_stream, stream.close):
        try:
            op()
        except OSError:
            pass


class WakeListener:
    """openWakeWord over a raw PyAudio stream. Owns the mic while DORMANT;
    releases it before a session pipeline opens it."""

    CHUNK = 1280                                # oWW's native 80 ms hop
    PREROLL_CHUNKS = 25                         # 2 s ring kept ahead of detection
    SILENT_CHUNKS = 375                         # 30 s of literal zeros = dead stream
    PEAK_HOPS = 15                              # 1.2 s of peak search, bench only

    # Hand-trained models live in the repo and travel by `git pull`: unlike the
    # pretrained set there is no upstream to re-fetch them from, so losing one
    # to a venv rebuild would mean retraining it.
    MODELS_DIR = Path(__file__).resolve().parent / "models"

    # Every tuning knob also as a CLASS default, all inert. __init__ replaces
    # each one, so these exist for the listener built by __new__ with no
    # config at all - test_preroll stubs one to drill stream death, and that
    # test has no business knowing which knobs the wake path happens to read.
    # The two dicts are read-only here; _patience always assigns fresh ones.
    vad_threshold = 0.0
    near_miss_factor = 0.0
    clips_keep = 0
    patience = {}
    patience_threshold = {}

    def __init__(self, pa, voice_cfg, input_device_index):
        import numpy as np
        from openwakeword.model import Model
        self.np = np
        self.pa = pa
        self.device_index = input_device_index
        self.model_name = voice_cfg["wakeModel"]          # e.g. hey_jarvis_v0.1
        self.key = self.model_name.rsplit("_v", 1)[0]     # e.g. hey_jarvis
        # EVERY tuning key below is optional with an off-or-inert default, and
        # none may join REQUIRED_VOICE: the K15's config.json is per-machine
        # and gitignored, so a key that is mandatory here is an agent that
        # will not start after a git pull until someone edits it by hand.
        # The two that change WHAT fires (vad, patience) default to off; the
        # two that only observe (near-miss, clips) default to on.
        self.vad_threshold = float(voice_cfg.get("wakeVadThreshold", 0) or 0)
        self.near_miss_factor = float(voice_cfg.get("wakeNearMissFactor", 0.5) or 0)
        self.clips_keep = int(voice_cfg.get("wakeClipsKeep", 200) or 0)
        self.model_path = self._resolve_model()
        self.model = Model(wakeword_models=[str(self.model_path)],
                           inference_framework="onnx",
                           vad_threshold=self.vad_threshold)
        self.patience, self.patience_threshold = self._patience(voice_cfg)

    def _patience(self, voice_cfg):
        """openWakeWord's N-of-last-N gate, as the (patience, threshold) pair
        predict() wants. Empty dicts mean the gate is off, which is also
        exactly what predict() expects for "behave as before".

        Three upstream rules the config cannot express. predict() RAISES
        unless a threshold dict accompanies patience. A patience of 1 is a
        no-op, since one frame trivially satisfies one. And the keys are
        openWakeWord's own names for the models - the ONNX basename - so they
        are read back off the loaded model rather than re-derived from
        wakeModel here; a key that does not match is not an error upstream,
        it silently skips the gate, which is the worst of both outcomes.

        Costs (n-1) hops of added latency, 80 ms each, on every detection.
        Capped at 30 because that is prediction_buffer's maxlen - a larger
        patience could never be satisfied."""
        n = min(int(voice_cfg.get("wakePatience", 0) or 0), 30)
        if n < 2:
            return {}, {}
        thr = float(voice_cfg["wakeThreshold"])
        return ({k: n for k in self.model.models},
                {k: thr for k in self.model.models})

    def _resolve_model(self):
        """Vendored model first, then openWakeWord's own resources dir
        (fetching a pretrained name on first run). Returns a PATH.

        The path matters: Model() resolves a bare NAME only against
        openWakeWord's six official models and raises ValueError on anything
        else - which behind the supervisor is a crash loop every 10 s rather
        than a message. A path it just loads, taking the model's name from the
        basename, which is why the naming convention is the whole interface.

        Downloads still target openwakeword's OWN package dir: it resolves the
        bundled feature extractors (melspectrogram, embedding) relative to that
        path, so pointing downloads at a custom directory strands them and the
        model loads against nothing. LOADING from elsewhere is unaffected - the
        extractors resolve from the package dir no matter where the wake model
        came from (measured 2026-08-13, both dirs, identical scores).
        """
        import openwakeword
        from openwakeword.utils import download_models
        res = Path(openwakeword.__file__).parent / "resources" / "models"
        vendored = self.MODELS_DIR / f"{self.model_name}.onnx"
        pretrained = res / f"{self.model_name}.onnx"

        # The mel + embedding extractors are shared by every wake model and
        # ship OUTSIDE the wheel, so they have to be fetched even when the
        # model itself is vendored - a rebuilt venv would otherwise load a
        # perfectly good custom model against nothing. download_models tops
        # them up whatever else it is asked for, and no-ops on a name it does
        # not recognise, so a custom key costs one stat call. silero_vad.onnx
        # rides the same fetch and is only REQUIRED when wakeVadThreshold is
        # on, because Model() loads the VAD eagerly then and dies without it.
        if not (res / "embedding_model.onnx").exists() or not (
                vendored.exists() or pretrained.exists()) or (
                self.vad_threshold > 0 and not (res / "silero_vad.onnx").exists()):
            log("wake_model_download", model=self.model_name,
                vendored=vendored.exists() or None)
            download_models([self.key])

        if vendored.exists():
            self.model_source = "vendored"
            return vendored
        self.model_source = "pretrained"
        if not pretrained.exists():
            # download_models silently no-ops on a name it does not know, so a
            # custom model that never landed (gitignored away, bad wakeModel)
            # arrives here rather than as an opaque ValueError from Model().
            log.error("wake_model_missing", model=self.model_name,
                      looked_in=[str(self.MODELS_DIR), str(res)])
            raise FileNotFoundError(f"{self.model_name}.onnx: not vendored in "
                                    f"{self.MODELS_DIR} and not a pretrained "
                                    f"openWakeWord model")
        return pretrained

    def rebind(self, pa, device_index):
        """Adopt a fresh PyAudio instance + re-resolved mic after an audio
        rebuild; the wake model and its state carry over untouched."""
        self.pa = pa
        self.device_index = device_index

    def score_chunk(self, chunk_int16):
        # max() over the dict because the KEY is openWakeWord's basename for
        # the model and this only ever loads one; patience/threshold are the
        # one place the key does matter, and _patience derives them from the
        # loaded model rather than from wakeModel. Both empty = stock predict.
        scores = self.model.predict(chunk_int16, patience=self.patience,
                                    threshold=self.patience_threshold)
        return max(scores.values())

    def _scan_peak(self, stream, score, hops):
        """Keep scoring past the crossing to find the peak. Bench paths ONLY.

        The score at the crossing is a first-crossing value, not a peak: it
        says how far up the ramp the threshold happened to sit. On 2026-08-15
        that made 15 genuine wakes (median 0.255) indistinguishable in the log
        from 3 false accepts (0.25 / 0.26 / 0.28) - the peak is the number
        that separates them, and it lands AFTER the crossing.

        So measuring it costs hops*80 ms. That is free in --wake-trials and
        --false-accept-soak, where nothing follows the detection, and
        unaffordable in the live loop, where a session is already being built
        and the user is mid-sentence. Hence peak_hops=0 there, and no peak
        field on the `wake` event."""
        peak = score
        for _ in range(hops):
            data = stream.read(self.CHUNK, exception_on_overflow=False)
            peak = max(peak, self.score_chunk(
                self.np.frombuffer(data, self.np.int16)))
        return peak

    def _open_stream(self):
        import pyaudio
        return self.pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                            input=True, frames_per_buffer=self.CHUNK,
                            input_device_index=self.device_index)

    def _listen(self, stream, threshold, on_score, ring, interrupt=None,
                peak_hops=0):
        """Blocks until the score crosses `threshold`; returns (score, peak),
        or (None, None) when `interrupt` asked to stop. peak == score unless
        peak_hops bought a real one (_scan_peak)."""
        silent = 0
        # A near miss is one contiguous run above the floor that never
        # crossed. Reported at the END of the run with the run's high-water
        # mark, so a single "hey alfred" that didn't take is one event and not
        # a dozen - and so the number in it is the one worth arguing about.
        floor = threshold * self.near_miss_factor
        episode = 0.0
        while True:
            data = stream.read(self.CHUNK, exception_on_overflow=False)
            # Something other than a wake word wants the session (today: an
            # announcement just finished and the follow-up window opens).
            # Checked per chunk, so it costs one 80 ms hop.
            if interrupt is not None and interrupt():
                return None, None
            chunk = self.np.frombuffer(data, self.np.int16)
            # Zombie watchdog: a WASAPI stream can outlive its endpoint (BT
            # profile flap) and keep delivering exact zeros forever - no error
            # to catch, just deafness (observed for 8.5 h). A real mic always
            # carries a noise floor, so a solid 30 s of literal zeros
            # means dead stream (or a hardware-muted mic, where a rebuild is
            # a harmless log line every 30 s). Raise into the same recovery
            # path as an honest stream death.
            silent = silent + 1 if not chunk.any() else 0
            if silent >= self.SILENT_CHUNKS:
                raise OSError("stream delivered only zeros for 30s - "
                              "endpoint presumed dead")
            if ring is not None:
                ring.append(data)
            score = self.score_chunk(chunk)
            if on_score:
                on_score(score)
            if score >= threshold:
                peak = self._scan_peak(stream, score, peak_hops)
                self.model.reset()
                return score, peak
            if floor and score >= floor:
                episode = max(episode, score)
            elif episode:
                # Recall's only trace. A wake word that does not fire emits
                # nothing at all, so every missed "hey alfred" on 2026-08-15
                # was invisible and the threshold argument was unfalsifiable.
                log("wake_near_miss", peak=round(episode, 3),
                    threshold=threshold,
                    shortfall=round(threshold - episode, 3))
                episode = 0.0

    def wait_for_wake(self, threshold, on_score=None, peak_hops=0):
        """Blocks until the wake word fires; returns (score, peak). The stream
        is closed before returning (trials/soak modes - no session follows),
        which is also why these are the paths that can afford peak_hops."""
        stream = self._open_stream()
        try:
            return self._listen(stream, threshold, on_score, None,
                                peak_hops=peak_hops)
        finally:
            close_stream_quietly(stream)

    def wait_for_wake_capture(self, threshold, on_quiet=None, interrupt=None):
        """Blocks until the wake word fires; returns (score, WakeCapture). The
        stream is handed to the capture - NOT closed - so speech overlapping or
        right after the wake phrase ("hey jarvis volume up", no pause) survives
        the session build. The ring seeds the capture with the ~2 s before
        detection, wake phrase included; strip_wake removes it text-side.
        on_quiet is the wake chime, fired by the capture when the user stops
        talking. Caller must stop() the capture (idempotent) to release the
        mic. Returns (None, None) if `interrupt` asked for a session instead -
        no wake phrase was spoken, so there is nothing to pre-roll."""
        stream = self._open_stream()
        ring = collections.deque(maxlen=self.PREROLL_CHUNKS)
        try:
            score, _peak = self._listen(stream, threshold, None, ring, interrupt)
        except Exception:
            close_stream_quietly(stream)
            raise
        if score is None:
            close_stream_quietly(stream)
            return None, None
        # Before WakeCapture, which is about to start consuming the stream -
        # but the ring itself is only READ here, so the capture still gets the
        # full pre-roll it seeds the session with.
        dump_clip(ring, score, self.clips_keep)
        return score, WakeCapture(stream, ring, on_quiet)
