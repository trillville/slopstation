"""C1 step-0 spike: does Pipecat's LocalAudioTransport behave on this Windows box?

Proves, with any USB mic + any speaker, on the actual K15:
  1. the pinned wheels import (PyAudio cp313, pipecat 1.7.0)
  2. PortAudio/WASAPI opens capture AND playback at 16 kHz mono
  3. capture actually flows (steady frame rate, live RMS readout)
  4. full duplex is stable: speech triggers a tone out the speaker WHILE
     capture keeps running (this is the barge-in prerequisite)
  5. a 10-minute soak survives without device errors

Run (on the K15, after copying the voice\\ folder to the Desktop):

    python -m venv %USERPROFILE%\\Desktop\\voice\\.venv
    %USERPROFILE%\\Desktop\\voice\\.venv\\Scripts\\pip install -r %USERPROFILE%\\Desktop\\voice\\requirements.txt
    %USERPROFILE%\\Desktop\\voice\\.venv\\Scripts\\python %USERPROFILE%\\Desktop\\voice\\spike.py

Speak a few times, leave it running ~10 minutes, Ctrl+C, paste ALL output back.
Optional arg = RMS speech threshold (default 900): spike.py 500 if it never
triggers, spike.py 2000 if it triggers on room noise.

Deliberately NO Silero/onnx here: a failure in this script can only mean the
audio transport. The speech trigger is plain RMS energy.
"""
import asyncio, platform, sys, time, traceback

RATE = 16000
TONE_HZ, TONE_S = 440, 0.25
RMS_THRESHOLD = 900  # optional argv[1] override, parsed under __main__ only -
#                      module-level parsing would crash `voice_agent --devices`
TRIGGER_COOLDOWN_S = 1.5
STATS_EVERY_S = 5.0
HOLDER = {}          # probe survives here so Ctrl+C still gets a summary


def banner():
    import pipecat
    import pyaudio
    print(f"[spike] python  : {sys.version}")
    print(f"[spike] platform: {platform.platform()}")
    print(f"[spike] pipecat : {pipecat.__version__}")
    print(f"[spike] pyaudio : {pyaudio.__version__}")
    print(f"[spike] rms threshold={RMS_THRESHOLD} (arg to change)")


def list_devices():
    """Raw PortAudio view, before pipecat touches anything - if this part
    fails, the wheel/driver layer is the problem, not the framework."""
    import pyaudio
    pa = pyaudio.PyAudio()
    print(f"[devices] host APIs: "
          + ", ".join(pa.get_host_api_info_by_index(i)["name"]
                      for i in range(pa.get_host_api_count())))
    def default_index(getter, what):
        try:
            return getter()["index"]
        except OSError:
            print(f"[devices] WARNING: no default {what} device - "
                  f"{'capture/duplex cannot run without a mic' if what == 'input' else 'tones will be inaudible'}")
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
        if i == idx_in:  mark += " <= default input"
        if i == idx_out: mark += " <= default output"
        print(f"[devices] {i:3d} {d['name']} ({', '.join(tags)}){mark}")
    pa.terminate()


def make_tone():
    import numpy as np
    t = np.arange(int(RATE * TONE_S)) / RATE
    wave = (np.sin(2 * np.pi * TONE_HZ * t) * 12000).astype(np.int16)
    return wave.tobytes()


async def main():
    import numpy as np
    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.transports.local.audio import (LocalAudioTransport,
                                                LocalAudioTransportParams)
    from pipecat.workers.runner import WorkerRunner

    tone = make_tone()

    class SpikeProbe(FrameProcessor):
        """Counts capture, fires a tone on speech. Swallows input-audio frames
        instead of forwarding (same pattern the real GrammarGate will use)."""
        def __init__(self):
            super().__init__()
            self.t0 = self.last_stats = time.monotonic()
            self.frames = self.bytes_in = self.events = self.tones = 0
            self.peak_rms = 0
            self.last_trigger = 0.0

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputAudioRawFrame):
                self.frames += 1
                self.bytes_in += len(frame.audio)
                rms = int(np.sqrt(np.mean(
                    np.frombuffer(frame.audio, np.int16).astype(np.float64) ** 2)))
                self.peak_rms = max(self.peak_rms, rms)
                now = time.monotonic()
                if rms >= RMS_THRESHOLD and now - self.last_trigger > TRIGGER_COOLDOWN_S:
                    self.last_trigger = now
                    self.events += 1
                    print(f"[spike] speech detected (rms={rms}) -> tone")
                    await self.push_frame(OutputAudioRawFrame(
                        audio=tone, sample_rate=RATE, num_channels=1))
                    self.tones += 1
                if now - self.last_stats >= STATS_EVERY_S:
                    dt = now - self.t0
                    print(f"[stats] {dt:5.0f}s frames={self.frames}"
                          f" ({self.frames/dt:.1f}/s) rms_now={rms}"
                          f" rms_peak={self.peak_rms} speech={self.events}"
                          f" tones={self.tones}")
                    self.last_stats = now
                return                      # swallow: no mic->speaker loop
            await self.push_frame(frame, direction)

    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True,  audio_in_sample_rate=RATE,
        audio_out_enabled=True, audio_out_sample_rate=RATE,
    ))
    probe = HOLDER["probe"] = SpikeProbe()
    worker = PipelineWorker(Pipeline([transport.input(), probe, transport.output()]))

    print("[spike] pipeline up - speak near the mic; expect a tone back.")
    print("[spike] leave running ~10 min, then Ctrl+C for the summary.")
    # handle_sigint=False: pipecat's signal hook uses loop.add_signal_handler,
    # which Windows' event loop doesn't implement - we catch KeyboardInterrupt.
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()

def summary(probe, err):
    print("\n========== SPIKE SUMMARY (paste everything back) ==========")
    if probe:
        dt = time.monotonic() - probe.t0
        ok_cap = probe.frames > 0 and probe.frames / max(dt, 1) > 10
        print(f"ran {dt:.0f}s | frames={probe.frames} ({probe.frames/max(dt,1):.1f}/s)"
              f" | peak_rms={probe.peak_rms} | speech events={probe.events}"
              f" | tones played={probe.tones}")
        print(f"capture flowing : {'PASS' if ok_cap else 'FAIL'}")
        print(f"duplex (tones)  : {'PASS' if probe.tones else 'UNTESTED - no speech trigger; retry with lower threshold'}")
    print(f"errors          : {err or 'none'}")
    print("===========================================================")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        RMS_THRESHOLD = int(sys.argv[1])
    err = None
    try:
        banner()
        list_devices()
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        err = traceback.format_exc()
        print(err)
    summary(HOLDER.get("probe"), err)
