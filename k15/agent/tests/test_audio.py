"""Blind test: device resolution never substitutes the system default for a
configured device that is merely absent. Fake device table, no PortAudio. Run:
    .venv\\Scripts\\python tests\\test_audio.py
"""
import sys

import _bootstrap  # noqa: F401

from agent.speech import audio
import cglib

VOICE = {"inputDeviceName": "ReSpeaker", "outputDeviceName": "ReSpeaker"}


class FakePA:
    """The two-line PyAudio surface resolve_device actually uses."""

    def __init__(self, names):
        self._d = [{"name": n, "maxInputChannels": 6, "maxOutputChannels": 2}
                   for n in names]
        self.terminated = False

    def get_device_count(self):
        return len(self._d)

    def get_device_info_by_index(self, i):
        return self._d[i]

    def terminate(self):
        self.terminated = True


def test_no_fragment_is_the_system_default():
    log = cglib.CapturingLog()
    idx = audio.resolve_device(FakePA(["Whatever"]), "", True, log=log)
    assert idx is None, "an unset fragment must still mean system default"
    assert log.find("audio_device"), log.events()
    print("  OK  unset fragment -> system default")


def test_present_device_resolves_to_its_index():
    log = cglib.CapturingLog()
    pa = FakePA(["Realtek", "Echo Cancelling Speakerphone (reSpeaker Flex)"])
    idx = audio.resolve_device(pa, "ReSpeaker", True, log=log)
    assert idx == 1, idx
    assert log.find("audio_device")[0]["index"] == 1
    print("  OK  present device -> index")


def test_absent_device_raises_instead_of_defaulting():
    log = cglib.CapturingLog()
    try:
        audio.resolve_device(FakePA(["Realtek"]), "ReSpeaker", True, log=log)
    except audio.DeviceMissing as e:
        assert e.kind == "input" and e.wanted == "ReSpeaker", (e.kind, e.wanted)
        assert log.find("audio_device_missing"), log.events()
        print("  OK  absent device -> DeviceMissing, not the default")
        return
    raise AssertionError("absent device silently resolved - this is the bug")


def test_announcer_keeps_the_lenient_answer():
    idx = audio.resolve_device(FakePA(["Realtek"]), "ReSpeaker", False,
                               log=None, required=False)
    assert idx is None, idx
    print("  OK  required=False -> system default (announce.py's path)")


def test_open_audio_waits_rather_than_binding_the_wrong_endpoint():
    """Recovery that resolves onto the system default reports success and
    stays deaf (62 rounds, 5 min 10 s). open_audio must return the real
    device or keep waiting."""
    log = cglib.CapturingLog()
    table = ["Realtek"]                         # array not back yet
    calls = []

    def fake_build(voice):
        calls.append(len(table))
        pa = FakePA(table)
        return (pa,
                audio.resolve_device(pa, voice["inputDeviceName"], True, log=log),
                audio.resolve_device(pa, voice["outputDeviceName"], False, log=log))

    slept = []

    def fake_sleep(s):
        slept.append(s)
        if len(slept) == 3:                     # the array comes back
            table.append("Echo Cancelling Speakerphone (reSpeaker Flex)")

    real_build, real_sleep = audio.build_audio, audio.time.sleep
    audio.build_audio, audio.time.sleep = fake_build, fake_sleep
    try:
        pa, input_idx, output_idx = audio.open_audio(VOICE, log=log)
    finally:
        audio.build_audio, audio.time.sleep = real_build, real_sleep

    assert input_idx == 1, f"bound {input_idx} - must be the array, never None"
    assert output_idx == 1, output_idx
    assert len(calls) == 4, f"expected 3 misses then a hit, got {len(calls)}"
    waits = log.find("audio_device_wait")
    assert waits, f"a multi-round outage logged no wait event: {log.events()}"
    assert waits[0]["waited_s"] == 0 and waits[0]["level"] == "error", waits[0]
    assert all(w["wanted"] == "ReSpeaker" for w in waits), waits
    print(f"  OK  open_audio waited {len(slept)} rounds, bound index "
          f"{input_idx} (never the default)")


def test_build_audio_terminates_the_instance_it_could_not_use():
    """open_audio retries every RETRY_S for the whole outage, so an instance
    stranded on the raise path leaks a handle per round."""
    made = []

    class FakePyAudioModule:
        @staticmethod
        def PyAudio():
            pa = FakePA(["Realtek"])            # array absent
            made.append(pa)
            return pa

    real = sys.modules.get("pyaudio")
    sys.modules["pyaudio"] = FakePyAudioModule
    try:
        for _ in range(3):
            try:
                audio.build_audio(VOICE)
            except audio.DeviceMissing:
                pass
    finally:
        if real is None:
            del sys.modules["pyaudio"]
        else:
            sys.modules["pyaudio"] = real

    assert len(made) == 3, made
    leaked = [pa for pa in made if not pa.terminated]
    assert not leaked, f"{len(leaked)} PortAudio instance(s) leaked on the raise path"
    print("  OK  build_audio terminates its instance before raising")


def main():
    for fn in (test_no_fragment_is_the_system_default,
               test_present_device_resolves_to_its_index,
               test_absent_device_raises_instead_of_defaulting,
               test_announcer_keeps_the_lenient_answer,
               test_build_audio_terminates_the_instance_it_could_not_use,
               test_open_audio_waits_rather_than_binding_the_wrong_endpoint):
        fn()
    print("OK - device resolution: absent != default, and recovery waits")


if __name__ == "__main__":
    main()
