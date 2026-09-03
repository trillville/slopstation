"""Device resolution never substitutes the system default for a
configured device that is merely absent. Fake device table, no PortAudio.
"""

import sys
import time

import pytest

from helpers import CapturingLog
from slopstation.agent.speech import audio

VOICE = {"inputDeviceName": "ReSpeaker", "outputDeviceName": "ReSpeaker"}


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
    stays deaf (62 rounds, 5 min 10 s). open_audio must return the real
    device or keep waiting."""
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
    monkeypatch.setattr(time, "sleep", fake_sleep)
    pa, input_idx, output_idx = audio.open_audio(VOICE, log=log)

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
