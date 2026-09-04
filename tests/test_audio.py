"""Test audio device resolution and wake-listener behavior with fake streams."""

import sys
import time
import wave

import numpy as np
import pytest

from helpers import CapturingLog
from slopstation.agent.speech import audio

VOICE = {"inputDeviceName": "ReSpeaker", "outputDeviceName": "ReSpeaker"}
CHUNK = audio.WakeListener.CHUNK


class FakePA:
    """The two-line PyAudio surface resolve_device actually uses. The table is
    copied at init, as PortAudio snapshots it: a device change must not show
    through an instance that predates it."""

    terminate_calls = 0

    def __init__(self, names):
        self._d: list[dict] = [
            {"name": n, "maxInputChannels": 6, "maxOutputChannels": 2} for n in names
        ]

    def get_device_count(self):
        return len(self._d)

    def get_device_info_by_index(self, i):
        return self._d[i]

    def terminate(self):
        self.terminate_calls += 1


def test_no_fragment_is_the_system_default():
    log = CapturingLog()
    idx = audio.resolve_device(FakePA(["Whatever"]), "", True, log=log)
    assert idx is None, "an unset fragment must still mean system default"
    assert log.find("audio_device"), log.events()


def test_present_device_resolves_to_its_index():
    log = CapturingLog()
    pa = FakePA(["Realtek", "Echo Cancelling Speakerphone (reSpeaker Flex)"])
    idx = audio.resolve_device(pa, "ReSpeaker", True, log=log)
    assert idx == 1, idx
    assert log.find("audio_device")[0]["index"] == 1


def test_absent_device_raises_instead_of_defaulting():
    log = CapturingLog()
    # A silently resolved absent device is the bug.
    with pytest.raises(audio.DeviceMissing) as e:
        audio.resolve_device(FakePA(["Realtek"]), "ReSpeaker", True, log=log)
    assert e.value.kind == "input" and e.value.wanted == "ReSpeaker", (
        e.value.kind,
        e.value.wanted,
    )
    assert log.find("audio_device_missing"), log.events()


def test_announcer_keeps_the_lenient_answer():
    idx = audio.resolve_device(
        FakePA(["Realtek"]), "ReSpeaker", False, log=None, required=False
    )
    assert idx is None, idx


def test_open_audio_waits_rather_than_binding_the_wrong_endpoint(monkeypatch):
    """Recovery that resolves onto the system default reports success and
    stays deaf. open_audio must return the real device or keep waiting."""
    log = CapturingLog()
    table = ["Realtek"]  # array not back yet
    calls = []

    def fake_build(voice):
        calls.append(len(table))
        pa = FakePA(table)
        return (
            pa,
            audio.resolve_device(pa, voice["inputDeviceName"], True, log=log),
            audio.resolve_device(pa, voice["outputDeviceName"], False, log=log),
        )

    slept = []

    def fake_sleep(s):
        slept.append(s)
        if len(slept) == 3:  # the array comes back
            table.append("Echo Cancelling Speakerphone (reSpeaker Flex)")

    monkeypatch.setattr(audio, "build_audio", fake_build)
    monkeypatch.setattr(audio, "log", log)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    pa, input_idx, output_idx = audio.open_audio(VOICE)

    assert input_idx == 1, f"bound {input_idx} - must be the array, never None"
    assert output_idx == 1, output_idx
    assert len(calls) == 4, f"expected 3 misses then a hit, got {len(calls)}"
    waits = log.find("audio_device_wait")
    assert waits, f"a multi-round outage logged no wait event: {log.events()}"
    assert waits[0]["waited_s"] == 0 and waits[0]["level"] == "error", waits[0]
    assert all(w["wanted"] == "ReSpeaker" for w in waits), waits


def test_build_audio_terminates_the_instance_it_could_not_use(monkeypatch):
    """open_audio retries every RETRY_S for the whole outage, so an instance
    stranded on the raise path leaks a handle per round."""
    made = []

    class FakePyAudioModule:
        @staticmethod
        def PyAudio():
            pa = FakePA(["Realtek"])  # array absent
            made.append(pa)
            return pa

    monkeypatch.setitem(sys.modules, "pyaudio", FakePyAudioModule)
    for _ in range(3):
        with pytest.raises(audio.DeviceMissing):
            audio.build_audio(VOICE)

    assert len(made) == 3, made
    leaked = [pa for pa in made if not pa.terminate_calls]
    assert not leaked, f"{len(leaked)} PortAudio instance(s) leaked on the raise path"


def bare_listener(**attrs):
    """A WakeListener without __init__ - no PortAudio, no model download - and
    the tuning knobs off. `attrs` is what the test wires in."""
    lst = audio.WakeListener.__new__(audio.WakeListener)
    vars(lst).update(near_miss_factor=0.0, patience={}, patience_threshold={})
    vars(lst).update(attrs)
    return lst


def test_dead_wake_stream_surfaces_original_error():
    """A -9999 mid-listen makes cleanup raise 'Stream not open', replacing the
    real error and escaping the handler. The original OSError must survive."""

    class DeadStream:
        def read(self, n, exception_on_overflow=True):
            raise OSError(-9999, "Unanticipated host error")

        def stop_stream(self):
            raise OSError("Stream not open")

        def close(self):
            raise OSError("Stream not open")

    class FakePA:
        def open(self, **kw):
            return DeadStream()

    lst = bare_listener(pa=FakePA(), device_index=None)
    with pytest.raises(OSError) as e:
        lst.wait_for_wake_capture(0.5)
    assert "Unanticipated" in str(e.value), f"original error was replaced: {e.value}"

    audio.close_stream_quietly(DeadStream())  # must not raise


def test_zombie_stream_trips_silence_watchdog():
    """After a device flap the reopened stream can deliver only zeros - no
    error, no wake. A solid run of zeros must raise into the OSError recovery
    path; real audio resets the count (a live mic has a noise floor)."""
    NOISY_AT = 10  # one real chunk mid-run resets the count

    class ZombieStream:
        n = 0

        def read(self, n, exception_on_overflow=True):
            self.n += 1
            if self.n == NOISY_AT:
                return b"\x01\x00" * n
            return b"\x00" * (n * 2)

        def stop_stream(self):
            pass

        def close(self):
            pass

    class FakeModel:
        # **kw tracks openWakeWord's real predict(), which also takes
        # patience/threshold/debounce_time - score_chunk passes the first two.
        def predict(self, chunk, **kw):
            return {"hey_jarvis": 0.0}

    lst = bare_listener(np=np, model=FakeModel())
    stream = ZombieStream()
    with pytest.raises(OSError, match="zeros"):
        lst._listen(stream, 0.5, None, None)
    want = NOISY_AT + audio.WakeListener.SILENT_CHUNKS
    assert stream.n == want, f"tripped after {stream.n} chunks, want {want}"


def test_near_miss_reports_one_event_per_run_with_its_peak(monkeypatch):
    """Recall's only trace: a wake word that does not fire emits nothing.
    One event per contiguous run above the floor, carrying that run's peak."""
    # floor = threshold 0.5 * factor 0.2 = 0.10. Two runs, then a crossing.
    scores = [
        0.02,
        0.12,
        0.28,
        0.19,
        0.03,  # run A, peak 0.28
        0.41,
        0.07,  # run B, peak 0.41
        0.55,
    ]  # crosses

    class ScriptedModel:
        i = 0
        reset_calls = 0

        def predict(self, chunk, **kw):
            self.i += 1
            return {"hey_jarvis": scores[self.i - 1]}

        def reset(self):
            self.reset_calls += 1

    class LiveStream:
        def read(self, n, exception_on_overflow=True):
            return b"\x01\x00" * n  # non-zero: watchdog stays quiet

    lst = bare_listener(np=np, model=ScriptedModel(), near_miss_factor=0.2)
    log = CapturingLog()
    monkeypatch.setattr(audio, "log", log)
    score = lst._listen(LiveStream(), 0.5, None, None)
    misses = log.find("wake_near_miss")

    assert score == 0.55, score
    assert len(misses) == 2, f"want one event per run, got {len(misses)}"
    assert [m["peak"] for m in misses] == [0.28, 0.41], misses
    assert misses[0]["shortfall"] == 0.22, misses[0]
    assert lst.model.reset_calls == 1, lst.model.reset_calls


def test_clip_dump_writes_prunes_and_never_raises(monkeypatch, tmp_path):
    """Wake clips are retained up to the configured limit."""
    ring = [b"\x01\x00" * CHUNK] * 3
    log = CapturingLog()
    monkeypatch.setattr(audio, "log", log)
    # clips_dir() lives under this test's own logs/, so nothing to re-point.
    for i in range(5):
        audio.dump_clip(ring, 0.20 + i / 100, keep=3)
    kept = sorted(audio.clips_dir().glob("wake-*.wav"))
    written = log.find("wake_clip")

    # keep=0 is the off switch: not even the directory.
    monkeypatch.setattr(audio, "clips_dir", lambda: tmp_path / "off")
    audio.dump_clip(ring, 0.5, keep=0)
    off_made = (tmp_path / "off").exists()

    # An unwritable clip directory does not stop wake detection.
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    monkeypatch.setattr(audio, "clips_dir", lambda: blocker / "wake")
    audio.dump_clip(ring, 0.5, keep=3)
    failures = log.find("wake_clip_failed")

    assert len(written) == 5, f"5 fires must log 5 clips, got {len(written)}"
    assert len(kept) == 3, f"keep=3 must prune to 3, got {len(kept)}"
    # Pruning keeps the newest, and names sort chronologically.
    assert [p.name.split("-")[-1] for p in kept] == [
        "0.220.wav",
        "0.230.wav",
        "0.240.wav",
    ], kept
    with wave.open(str(kept[0]), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert w.getnframes() == 3 * CHUNK, w.getnframes()
    assert not off_made, "keep=0 must not create the directory"
    assert len(failures) == 1 and failures[0]["level"] == "warn", failures
