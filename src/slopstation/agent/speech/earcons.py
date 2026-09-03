"""Earcons - the short tones the system answers with instead of speech:
count vocabulary, session bookends, announcement cue. Synthesized from
specs, so no binary assets; all PCM is SAMPLE_RATE mono s16le, ready to
wrap in an OutputAudioRawFrame.

The counts are the contract, mirroring the haptic thuds: 1 = accepted,
2 = busy, 3 = failed. A grammar-gate ack must be instant - never
synthesize speech for it; pcm() is a dict lookup after first use.
"""

import numpy as np

SAMPLE_RATE = 16000
GAP_MS = 70  # silence between the bursts of a counted earcon
ATTACK_MS = 5  # per-note fade-in (click prevention)
# per-note fade-out to true zero - a decaying note still clicks if it is simply
# truncated
RELEASE_MS = 10

# Bell timbre: partials decaying faster the higher they are.
PARTIALS = ((1, 1.00), (2, 0.22), (3, 0.07))

GAIN = 1.0  # global volume knob, config voice.earconGain


def _seq(bursts, gap_ms=GAP_MS):
    """Counted earcon -> notes at start offsets. Bursts never overlap: the
    silence between them is what makes the count recoverable, by ear and by
    test_earcons."""
    notes, t = [], 0
    for freq, dur in bursts:
        notes.append((freq, t, dur))
        t += dur + gap_ms
    return notes


# name -> (peak amplitude, decay tau as a fraction of note duration,
#          [(freq_hz, start_ms, dur_ms), ...])
SPECS = {
    # Bookends: ascending fifth to open, the same fifth descending to close.
    "wake": (3800, 0.32, [(784.0, 0, 200), (1174.7, 70, 310)]),
    "close": (2400, 0.32, [(1174.7, 0, 200), (784.0, 70, 340)]),
    # The count vocabulary: ok = one bell on the wake chime's note, busy = the
    # same note twice, fail = three falling.
    "ok": (5200, 0.30, _seq([(1174.7, 260)])),
    "busy": (4600, 0.26, _seq([(880.0, 150), (880.0, 150)])),
    "fail": (4600, 0.26, _seq([(698.5, 160), (587.3, 160), (440.0, 160)], 60)),
    # Loudest: it arrives unasked, across the room.
    "announce": (6200, 0.30, _seq([(987.8, 200), (1318.5, 300)])),
}

_cache: dict = {}


def set_gain(gain):
    """Global earcon volume (config voice.earconGain; 1.0 = as spec'd)."""
    global GAIN
    gain = max(0.0, float(gain))
    if gain != GAIN:
        _cache.clear()
        GAIN = gain


def _note(freq, dur_ms, decay):
    """One note as a float array peaking near 1.0, silent at both ends.
    `decay` is tau as a fraction of the duration."""
    n = int(SAMPLE_RATE * dur_ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    tau = decay * dur_ms / 1000
    wave = np.zeros(n)
    for k, amp in PARTIALS:
        if freq * k < SAMPLE_RATE / 2:  # above Nyquist it aliases down
            wave += amp * np.sin(2 * np.pi * freq * k * t) * np.exp(-t * k / tau)
    a = min(int(SAMPLE_RATE * ATTACK_MS / 1000), n // 2)
    r = min(int(SAMPLE_RATE * RELEASE_MS / 1000), n // 2)
    if a:
        wave[:a] *= np.linspace(0.0, 1.0, a)
    if r:
        wave[-r:] *= np.linspace(1.0, 0.0, r)
    return wave


def samples(name):
    """The earcon as an int16 numpy array (cached)."""
    if name not in _cache:
        amp, decay, notes = SPECS[name]
        starts = [int(SAMPLE_RATE * start / 1000) for _, start, _ in notes]
        waves = [_note(freq, dur, decay) for freq, _, dur in notes]
        mix = np.zeros(max(i + len(w) for i, w in zip(starts, waves, strict=True)))
        for i, w in zip(starts, waves, strict=True):
            mix[i : i + len(w)] += w
        # Normalize to the spec'd peak: an amplitude means the same thing
        # across earcons regardless of partial/note count.
        mix *= amp * GAIN / max(float(np.max(np.abs(mix))), 1e-9)
        _cache[name] = np.clip(mix, -32767, 32767).astype(np.int16)
    return _cache[name]


def pcm(name):
    """The earcon as raw s16le bytes for an output audio frame."""
    return samples(name).tobytes()
