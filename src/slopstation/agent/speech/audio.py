"""One PortAudio world: device resolution, recovery, out-of-session playback,
and the wake listener. Invariant: NEVER resolve a device against a PyAudio
instance that predates a device change - PortAudio snapshots the device table
at init.
"""

from __future__ import annotations

import collections
import time
import wave
from pathlib import Path

from slopstation import logbook, paths
from slopstation.agent.speech import earcons
from slopstation.agent.speech.preroll import WakeCapture

log = logbook.logger("voice")

RETRY_S = 5  # device-wait poll interval
WAIT_QUIET_S = 30  # re-log an ongoing wait this often


# Fired pre-roll, kept on disk. Under logs/ so .gitignore already covers it.
def clips_dir():
    return paths.logs() / "wake"


class DeviceMissing(Exception):
    """A device name-fragment IS configured but nothing matches it right now.
    Distinct from "no fragment configured", which means the system default."""

    def __init__(self, kind: str, wanted: str) -> None:
        super().__init__(f"no {kind} device matching {wanted!r}")
        self.kind = kind
        self.wanted = wanted


def model_key(model_name: str) -> str:
    """ "hey_jarvis_v0.1" -> "hey_jarvis": the stem openWakeWord's download and
    WakeListener.key go by."""
    return model_name.rsplit("_v", 1)[0]


def wake_phrase(model_name: str) -> str:
    """ "hey_jarvis_v0.1" -> "hey jarvis": what the pre-roll transcribes and
    strip_wake anchors on."""
    return model_key(model_name).replace("_", " ")


def resolve_device(
    pa, fragment: str | None, want_input: bool, log=log, required: bool = True
) -> int | None:
    """Config name-fragment -> PyAudio device index; None = system default.
    Logs the bound NAME: after a rebuild the index alone says nothing about
    which physical endpoint is live. log=None resolves silently. An absent
    configured device raises DeviceMissing rather than quietly becoming the
    default; required=False keeps the lenient answer for announce.py."""
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


def list_devices() -> None:
    """Raw PortAudio view for --devices: every endpoint with its channel
    counts and the two system defaults. config's inputDeviceName /
    outputDeviceName take a unique fragment of a name; empty = the default."""
    import pyaudio

    pa = pyaudio.PyAudio()
    print(
        "[devices] host APIs: "
        + ", ".join(
            pa.get_host_api_info_by_index(i)["name"]
            for i in range(pa.get_host_api_count())
        )
    )

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


def build_audio(voice: dict) -> tuple:
    """A fresh PyAudio instance plus both devices resolved by name - the only
    way to see endpoints that (re)appeared since the last one. Raises
    DeviceMissing if a configured device is not in the table, tearing the
    instance down first: open_audio retries every RETRY_S, so a leak would
    strand a host handle per round."""
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        return (
            pa,
            resolve_device(pa, voice.get("inputDeviceName"), want_input=True),
            resolve_device(pa, voice.get("outputDeviceName"), want_input=False),
        )
    except Exception:
        try:
            pa.terminate()
        except Exception as e:
            log.warn("audio_teardown_failed", err=str(e))
        raise


def open_audio(voice: dict) -> tuple:
    """build_audio, but WAITING for a configured device that isn't there yet
    rather than settling for the Windows default: a silent fallback leaves the
    wake loop deaf on the wrong endpoint. Same policy for startup and
    recovery; loops until the real device answers."""
    waited = 0.0
    while True:
        try:
            return build_audio(voice)
        except DeviceMissing as e:
            # First miss, then every WAIT_QUIET_S: an outage is one event with
            # a duration, not a scroll of identical lines.
            if waited % WAIT_QUIET_S < RETRY_S:
                log.error(
                    "audio_device_wait",
                    kind=e.kind,
                    wanted=e.wanted,
                    waited_s=round(waited),
                    retry_s=RETRY_S,
                )
        except Exception as e:
            log.error("audio_rebuild_failed", err=str(e), retry_s=RETRY_S)
        time.sleep(RETRY_S)
        waited += RETRY_S


def rebuild_audio(old_pa, voice: dict, listener) -> tuple:
    """Recovery from a dead wake stream: tear the whole PortAudio instance
    down and rebuild against the current device table. Reopening on the old
    instance retries a stale index - a reconnected headset gets a NEW one the
    old snapshot can't see."""
    try:
        old_pa.terminate()
    except Exception as e:
        log.warn("audio_teardown_failed", err=str(e))
    time.sleep(RETRY_S)  # let the endpoint settle first
    pa, input_idx, output_idx = open_audio(voice)
    listener.rebind(pa, input_idx)
    return pa, input_idx, output_idx


def play_pcm(pa, pcm: bytes, device_index: int | None = None) -> None:
    """Blocking playback for the earcons outside a session (wake chime before
    the pipeline is up, sleep chime after teardown). One retry after a settle:
    Bluetooth outputs renegotiate profiles around our stream churn and can
    transiently refuse to open (-9999). Never fatal."""
    import pyaudio

    for attempt in (1, 2):
        try:
            s = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=earcons.SAMPLE_RATE,
                output=True,
                output_device_index=device_index,
            )
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

    openWakeWord's custom verifier trains on real false activations, and a
    transcript cannot be re-scored. This is the PRE-detection window, so
    replaying it reproduces the score up to the crossing and NOT the peak
    (_scan_peak). Local only, never uploaded. Fail-soft."""
    if keep <= 0 or not ring:
        return
    try:
        clips_dir().mkdir(parents=True, exist_ok=True)
        # Milliseconds are load-bearing: the prune below trusts names to sort
        # chronologically, and two fires in one second would collide. Same
        # `now` as the seconds field so the tiebreaker cannot wrap.
        now = time.time()
        name = (
            f"wake-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"
            f"-{int(now * 1000) % 1000:03d}-{score:.3f}.wav"
        )
        with wave.open(str(clips_dir() / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"".join(ring))
        # Sorted names are chronological, so the oldest are the head.
        for old in sorted(clips_dir().glob("wake-*.wav"))[:-keep]:
            old.unlink(missing_ok=True)
        log("wake_clip", clip=name, secs=round(len(ring) * 0.08, 1))
    except Exception as e:
        log.warn("wake_clip_failed", err=str(e))


def close_stream_quietly(stream) -> None:
    """Best-effort close for a possibly-dead stream: after a -9999 host error
    the stream is already torn down and stop/close themselves raise 'Stream
    not open', replacing the original error."""
    for op in (stream.stop_stream, stream.close):
        try:
            op()
        except OSError:
            pass


class WakeListener:
    """openWakeWord over a raw PyAudio stream. Owns the mic while DORMANT;
    releases it before a session pipeline opens it."""

    CHUNK = 1280  # oWW's native 80 ms hop
    PREROLL_CHUNKS = 25  # 2 s ring kept ahead of detection
    SILENT_CHUNKS = 375  # 30 s of literal zeros = dead stream
    PEAK_HOPS = 15  # 1.2 s of peak search, bench only

    # Hand-trained models live in the repo: no upstream to re-fetch them from.
    MODELS_DIR = Path(__file__).resolve().parents[1] / "models"

    def __init__(self, pa, voice_cfg, input_device_index):
        import numpy as np
        from openwakeword.model import Model

        self.np = np
        self.pa = pa
        self.device_index = input_device_index
        self.model_name = voice_cfg["wakeModel"]  # e.g. hey_jarvis_v0.1
        self.key = model_key(self.model_name)  # e.g. hey_jarvis
        # EVERY tuning key below is optional with an off-or-inert default and
        # none may join REQUIRED_VOICE: the K15's config.json is per-machine
        # and gitignored, so a mandatory key means an agent that will not start
        # after a git pull.
        self.vad_threshold = float(voice_cfg.get("wakeVadThreshold", 0) or 0)
        self.near_miss_factor = float(voice_cfg.get("wakeNearMissFactor", 0.5) or 0)
        self.clips_keep = int(voice_cfg.get("wakeClipsKeep", 200) or 0)
        self.model_path = self._resolve_model()
        self.model = Model(
            wakeword_models=[str(self.model_path)],
            inference_framework="onnx",
            vad_threshold=self.vad_threshold,
            custom_verifier_models=self._verifier(voice_cfg),
            custom_verifier_threshold=float(
                voice_cfg.get("wakeVerifierThreshold", 0.1) or 0.1
            ),
        )
        self.patience, self.patience_threshold = self._patience(voice_cfg)

    def _verifier(self, voice_cfg):
        """{model_key: path} for openWakeWord's second stage, or {} for off.
        It replaces the score, so enabling it voids wakeThreshold."""
        name = voice_cfg.get("wakeVerifier")
        if not name:
            return {}
        path = self.MODELS_DIR / name
        if not path.exists():
            log.error(
                "wake_verifier_missing", verifier=name, looked_in=str(self.MODELS_DIR)
            )
            raise FileNotFoundError(f"{name}: not vendored in {self.MODELS_DIR}")
        log("wake_verifier", verifier=name, model=self.model_path.stem)
        return {self.model_path.stem: str(path)}

    def _patience(self, voice_cfg):
        """openWakeWord's N-of-N gate; predict() needs both dicts or raises;
        keys are the loaded model's names."""
        n = min(int(voice_cfg.get("wakePatience", 0) or 0), 30)
        if n < 2:
            return {}, {}
        thr = float(voice_cfg["wakeThreshold"])
        return ({k: n for k in self.model.models}, {k: thr for k in self.model.models})

    def _resolve_model(self):
        """Vendored model first, then openWakeWord's own resources dir
        (fetching a pretrained name on first run). Returns a PATH: Model()
        resolves a bare NAME only against the six official models and raises
        ValueError on anything else, while a path it just loads, naming the
        model after the basename. Downloads still target openwakeword's OWN
        package dir - it resolves the bundled feature extractors relative to
        that path. LOADING from elsewhere is unaffected."""
        import openwakeword
        from openwakeword.utils import download_models

        res = Path(openwakeword.__file__).parent / "resources" / "models"
        vendored = self.MODELS_DIR / f"{self.model_name}.onnx"
        pretrained = res / f"{self.model_name}.onnx"

        # The mel + embedding extractors ship OUTSIDE the wheel, so they must
        # be fetched even for a vendored model; download_models no-ops on an
        # unrecognised name. silero_vad.onnx rides the same fetch, REQUIRED
        # only when wakeVadThreshold is on - Model() then loads it eagerly.
        if (
            not (res / "embedding_model.onnx").exists()
            or not (vendored.exists() or pretrained.exists())
            or (self.vad_threshold > 0 and not (res / "silero_vad.onnx").exists())
        ):
            log(
                "wake_model_download",
                model=self.model_name,
                vendored=vendored.exists() or None,
            )
            download_models([self.key])

        if vendored.exists():
            self.model_source = "vendored"
            return vendored
        self.model_source = "pretrained"
        if not pretrained.exists():
            # download_models silently no-ops on an unknown name, so a custom
            # model that never landed arrives here, not as a ValueError.
            log.error(
                "wake_model_missing",
                model=self.model_name,
                looked_in=[str(self.MODELS_DIR), str(res)],
            )
            raise FileNotFoundError(
                f"{self.model_name}.onnx: not vendored in "
                f"{self.MODELS_DIR} and not a pretrained "
                f"openWakeWord model"
            )
        return pretrained

    def rebind(self, pa, device_index: int | None) -> None:
        """Adopt a fresh PyAudio instance + mic index after an audio rebuild;
        the wake model and its state carry over untouched."""
        self.pa = pa
        self.device_index = device_index

    def score_chunk(self, chunk_int16) -> float:
        # Only one model is ever loaded. Both dicts empty = stock predict.
        scores = self.model.predict(
            chunk_int16, patience=self.patience, threshold=self.patience_threshold
        )
        return max(scores.values())

    def _scan_peak(self, stream, score, hops):
        """Score past the crossing to find the peak; costs hops x 80 ms, so
        bench only."""
        peak = score
        for _ in range(hops):
            data = stream.read(self.CHUNK, exception_on_overflow=False)
            peak = max(peak, self.score_chunk(self.np.frombuffer(data, self.np.int16)))
        return peak

    def _open_stream(self):
        import pyaudio

        return self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=self.CHUNK,
            input_device_index=self.device_index,
        )

    def _listen(self, stream, threshold, ring, interrupt=None, peak_hops=0):
        """Blocks until the score crosses `threshold`; returns (score, peak),
        or (None, None) when `interrupt` asked to stop. peak == score unless
        peak_hops bought a real one (_scan_peak)."""
        silent = 0
        # A near miss is one contiguous run above the floor that never crossed,
        # reported at the END with its high-water mark: one event, not a dozen.
        floor = threshold * self.near_miss_factor
        episode = 0.0
        while True:
            data = stream.read(self.CHUNK, exception_on_overflow=False)
            # Something other than a wake word wants the session (an
            # announcement's follow-up window). One 80 ms hop of latency.
            if interrupt is not None and interrupt():
                return None, None
            chunk = self.np.frombuffer(data, self.np.int16)
            # Zombie watchdog: a WASAPI stream can outlive its endpoint (BT
            # profile flap) and deliver exact zeros forever with no error to
            # catch. A real mic has a noise floor, so 30 s of zeros means a
            # dead stream; raise into the recovery path.
            silent = silent + 1 if not chunk.any() else 0
            if silent >= self.SILENT_CHUNKS:
                raise OSError(
                    "stream delivered only zeros for 30s - endpoint presumed dead"
                )
            if ring is not None:
                ring.append(data)
            score = self.score_chunk(chunk)
            if score >= threshold:
                peak = self._scan_peak(stream, score, peak_hops)
                self.model.reset()
                return score, peak
            if floor and score >= floor:
                episode = max(episode, score)
            elif episode:
                # Recall's only trace: a wake word that does not fire is silent.
                log(
                    "wake_near_miss",
                    peak=round(episode, 3),
                    threshold=threshold,
                    shortfall=round(threshold - episode, 3),
                )
                episode = 0.0

    def wait_for_wake(self, threshold: float, peak_hops: int = 0) -> tuple:
        """Blocks until the wake word fires; returns (score, peak). Closes the
        stream - trials/soak only, no session follows, hence peak_hops."""
        stream = self._open_stream()
        try:
            return self._listen(stream, threshold, None, peak_hops=peak_hops)
        finally:
            close_stream_quietly(stream)

    def wait_for_wake_capture(
        self, threshold: float, on_quiet=None, interrupt=None
    ) -> tuple:
        """Blocks until the wake word fires; returns (score, WakeCapture). The
        stream is handed to the capture - NOT closed - so speech right after
        the wake phrase survives the session build. The ring seeds the capture
        with the ~2 s before detection, wake phrase included (strip_wake
        removes it text-side). on_quiet fires when the user stops talking; the
        caller must stop() the capture (idempotent) to release the mic.
        (None, None) if `interrupt` asked for a session instead."""
        stream = self._open_stream()
        ring: collections.deque[bytes] = collections.deque(maxlen=self.PREROLL_CHUNKS)
        try:
            score, _peak = self._listen(stream, threshold, ring, interrupt)
        except Exception:
            close_stream_quietly(stream)
            raise
        if score is None:
            close_stream_quietly(stream)
            return None, None
        # Only READS the ring, so the capture still gets the full pre-roll.
        dump_clip(ring, score, self.clips_keep)
        return score, WakeCapture(stream, ring, on_quiet)
