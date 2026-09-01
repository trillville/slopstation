"""Blind test: device resolution never substitutes the system default for a
configured device that is merely absent, and a pinned host API takes that
API's copy of the endpoint. Fake device table, no PortAudio. Run:
    .venv\\Scripts\\python tests\\test_audio.py
"""
import sys

import _bootstrap  # noqa: F401

from agent.speech import audio
import cglib

VOICE = {"inputDeviceName": "ReSpeaker", "outputDeviceName": "ReSpeaker"}


class FakePA:
    """The two-line PyAudio surface resolve_device actually uses."""

    HOST_APIS = ("MME", "Windows DirectSound", "Windows WASAPI")

    def __init__(self, names, host_apis=None):
        # Windows enumerates the same endpoint once per host API; host_apis[i]
        # is the API device i came from. All-MME by default, which keeps the
        # unpinned tests reading as a flat table.
        apis = host_apis if host_apis is not None else [0] * len(names)
        self._d = [{"name": n, "maxInputChannels": 6, "maxOutputChannels": 2,
                    "hostApi": a} for n, a in zip(names, apis)]
        self.terminated = False

    def get_host_api_count(self):
        return len(self.HOST_APIS)

    def get_host_api_info_by_index(self, i):
        return {"name": self.HOST_APIS[i], "index": i}

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


def test_host_api_pin_takes_that_apis_copy_not_the_first():
    """PortAudio sorts MME first, so the unpinned resolve always lands on MME -
    the wake lane's binding, which nobody chose (154 -9999 deaths, 2026-08-11
    to 08-31)."""
    log = cglib.CapturingLog()
    pa = FakePA(["Echo Cancelling Speakerphone (reSpeaker)"] * 3,
                host_apis=[0, 1, 2])
    assert audio.resolve_device(pa, "ReSpeaker", True, log=None) == 0,         "unpinned must still take the first hit - that is the behaviour pinned"
    idx = audio.resolve_device(pa, "ReSpeaker", True, log=log,
                               host_api="Windows WASAPI")
    assert idx == 2, idx
    assert log.find("audio_device")[0]["index"] == 2, log.events()
    print("  OK  host_api pin -> WASAPI's copy, not MME's")


def test_unhonourable_host_api_matches_nothing():
    """A pin PortAudio cannot honour must NOT fall through to the unpinned
    first hit: that silently restores the binding the pin exists to replace."""
    pa = FakePA(["Echo Cancelling Speakerphone (reSpeaker)"], host_apis=[0])
    idx = audio.resolve_device(pa, "ReSpeaker", True, log=None,
                               required=False, host_api="ALSA")
    assert idx is None, idx
    print("  OK  host API PortAudio lacks -> no match, not the MME fallback")


def test_mono_view_takes_channel_0_and_survives_a_short_read():
    """WASAPI shared mode opens at the native 6 ch, while the ring, dump_clip's
    1-channel wav and the PCM handed to the STT are all mono bytes."""
    import numpy as np

    class FakeStream:
        trim = None

        def __init__(self):
            self.stopped = self.closed = False

        def read(self, frames, exception_on_overflow=True):
            # ch0 counts up; the other five are constant, so a downmix that
            # averaged instead of selecting would not produce 0,1,2,3.
            f = np.empty(frames * 6, np.int16)
            for c in range(6):
                f[c::6] = np.arange(frames, dtype=np.int16) if c == 0 else -c
            return f.tobytes()[:self.trim]

        def stop_stream(self):
            self.stopped = True

        def close(self):
            self.closed = True

    s = FakeStream()
    view = audio._MonoView(s, 6)
    got = np.frombuffer(view.read(4), np.int16).tolist()
    assert got == [0, 1, 2, 3], got
    view.stop_stream()
    view.close()
    assert s.stopped and s.closed, (s.stopped, s.closed)

    # 7 samples is not a whole 6-channel frame: strided slicing yields ch0 of
    # the two complete frames, where a reshape would raise ValueError and
    # escape the recovery path, which catches OSError.
    short = FakeStream()
    short.trim = 7 * 2
    got = np.frombuffer(audio._MonoView(short, 6).read(4), np.int16).tolist()
    assert got == [0, 1], got
    print("  OK  _MonoView -> ch0, stop/close pass through, short read safe")


def main():
    for fn in (test_no_fragment_is_the_system_default,
               test_present_device_resolves_to_its_index,
               test_absent_device_raises_instead_of_defaulting,
               test_announcer_keeps_the_lenient_answer,
               test_host_api_pin_takes_that_apis_copy_not_the_first,
               test_unhonourable_host_api_matches_nothing,
               test_mono_view_takes_channel_0_and_survives_a_short_read,
               test_build_audio_terminates_the_instance_it_could_not_use,
               test_open_audio_waits_rather_than_binding_the_wrong_endpoint):
        fn()
    print("OK - device resolution: absent != default, a pin is honoured or "
          "misses, and recovery waits")


if __name__ == "__main__":
    main()
