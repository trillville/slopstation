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
from pathlib import Path

import cglib
import earcons
from preroll import WakeCapture

log = cglib.make_log("voice")

RETRY_S = 5                                     # device-wait poll interval
WAIT_QUIET_S = 30                               # re-log an ongoing wait this often


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

    def __init__(self, pa, voice_cfg, input_device_index):
        import numpy as np
        from openwakeword.model import Model
        self.np = np
        self.pa = pa
        self.device_index = input_device_index
        self.model_name = voice_cfg["wakeModel"]          # e.g. hey_jarvis_v0.1
        self.key = self.model_name.rsplit("_v", 1)[0]     # e.g. hey_jarvis
        self._ensure_model()
        self.model = Model(wakeword_models=[self.key], inference_framework="onnx")

    def _ensure_model(self):
        # Models must land in openwakeword's OWN package dir: it resolves the
        # bundled feature extractors (melspectrogram, embedding) relative to
        # that path, so pointing downloads at a custom directory strands them
        # and the model loads against nothing.
        import openwakeword
        from openwakeword.utils import download_models
        res = Path(openwakeword.__file__).parent / "resources" / "models"
        if not (res / f"{self.model_name}.onnx").exists():
            log("wake_model_download", model=self.model_name)
            download_models([self.key])

    def rebind(self, pa, device_index):
        """Adopt a fresh PyAudio instance + re-resolved mic after an audio
        rebuild; the wake model and its state carry over untouched."""
        self.pa = pa
        self.device_index = device_index

    def score_chunk(self, chunk_int16):
        scores = self.model.predict(chunk_int16)
        return max(scores.values())

    def _open_stream(self):
        import pyaudio
        return self.pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                            input=True, frames_per_buffer=self.CHUNK,
                            input_device_index=self.device_index)

    def _listen(self, stream, threshold, on_score, ring, interrupt=None):
        silent = 0
        while True:
            data = stream.read(self.CHUNK, exception_on_overflow=False)
            # Something other than a wake word wants the session (today: an
            # announcement just finished and the follow-up window opens).
            # Checked per chunk, so it costs one 80 ms hop.
            if interrupt is not None and interrupt():
                return None
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
                self.model.reset()
                return score

    def wait_for_wake(self, threshold, on_score=None):
        """Blocks until the wake word fires; returns the score. The stream is
        closed before returning (trials/soak modes - no session follows)."""
        stream = self._open_stream()
        try:
            return self._listen(stream, threshold, on_score, None)
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
            score = self._listen(stream, threshold, None, ring, interrupt)
        except Exception:
            close_stream_quietly(stream)
            raise
        if score is None:
            close_stream_quietly(stream)
            return None, None
        return score, WakeCapture(stream, ring, on_quiet)
