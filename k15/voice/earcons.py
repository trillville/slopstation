"""Earcon synthesis: the count vocabulary as audio, mirroring the haptic
thuds - 1 = accepted/launching, 2 = busy, 3 = failed - plus a wake tick
("listening") and a soft session-close tick. Synthesized at import from specs;
no binary assets in the repo. All PCM is SAMPLE_RATE mono s16le, ready to wrap
in an OutputAudioRawFrame.

Tier-1 acks must be instant: never synthesize speech for them, never wait on
anything - pcm() is a dict lookup after first use.
"""
import numpy as np

SAMPLE_RATE = 16000
GAP_MS = 70          # silence between bursts of one earcon
FADE_MS = 5          # per-burst fade in/out (click prevention)

# name -> (amplitude, [(freq_hz, duration_ms), ...])  - burst count carries
# the meaning; frequencies just make the counts pleasant to tell apart.
SPECS = {
    "wake":  (7000, [(1175, 45)]),
    "ok":    (9000, [(660, 90)]),
    "busy":  (9000, [(520, 70), (520, 70)]),
    "fail":  (9000, [(330, 90), (330, 90), (330, 90)]),
    "close": (4500, [(440, 55)]),
}

_cache = {}


def _burst(freq, dur_ms, amp):
    n = int(SAMPLE_RATE * dur_ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    wave = np.sin(2 * np.pi * freq * t) * amp
    fade = min(int(SAMPLE_RATE * FADE_MS / 1000), n // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade)
        wave[:fade] *= ramp
        wave[-fade:] *= ramp[::-1]
    return wave


def samples(name):
    """The earcon as an int16 numpy array (cached)."""
    if name not in _cache:
        amp, bursts = SPECS[name]
        gap = np.zeros(int(SAMPLE_RATE * GAP_MS / 1000))
        parts = []
        for i, (freq, dur) in enumerate(bursts):
            if i:
                parts.append(gap)
            parts.append(_burst(freq, dur, amp))
        _cache[name] = np.concatenate(parts).astype(np.int16)
    return _cache[name]


def pcm(name):
    """The earcon as raw s16le bytes for an output audio frame."""
    return samples(name).tobytes()
