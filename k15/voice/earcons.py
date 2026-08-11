"""Earcon synthesis: the count vocabulary as audio, mirroring the haptic
thuds - 1 = accepted/launching, 2 = busy, 3 = failed - plus the two session
bookends (a wake chime and its mirror at sleep) and the assistant lane's
think tick and announcement cue. Synthesized at import from specs; no binary
assets in the repo. All PCM is SAMPLE_RATE mono s16le, ready to wrap in an
OutputAudioRawFrame.

Two voices out of one engine:

  bells (wake, close) - overlapping notes, a few light partials, exponential
    decay. Deliberately quiet: they fire on every single wake, and a flat sine
    at tick level is exactly what made the old one jarring. A decaying note is
    perceptually much softer than a flat one of the same peak, so these read
    quieter than their amplitudes alone suggest.

  flat tones (the rest) - pitches, counts and level unchanged. The counts are
    the contract (they mirror the haptic thuds and the ear is trained on
    them); only the release got marginally softer.

Tier-1 acks must be instant: never synthesize speech for them, never wait on
anything - pcm() is a dict lookup after first use.
"""
import numpy as np

SAMPLE_RATE = 16000
GAP_MS = 70          # silence between the bursts of a counted earcon
ATTACK_MS = 5        # per-note fade-in (click prevention)
RELEASE_MS = 10      # per-note fade-out to true zero - a decaying note still
                     # clicks if it is simply truncated

# Bell timbre: a few light partials, each decaying faster than the one below
# it, as a struck object does. Enough harmonic content to sound like a thing
# rather than a test tone; not enough to be bright or harsh.
PARTIALS = ((1, 1.00), (2, 0.22), (3, 0.07))

GAIN = 1.0           # global volume knob, config voice.earconGain


def _seq(bursts, gap_ms=GAP_MS):
    """Counted earcon -> notes at start offsets. The SILENCE between bursts is
    what makes the count recoverable - by ear and by test_earcons - so these
    never overlap."""
    notes, t = [], 0
    for freq, dur in bursts:
        notes.append((freq, t, dur))
        t += dur + gap_ms
    return notes


# name -> (peak amplitude, decay tau as a fraction of note duration - None for
#          a flat tone -, [(freq_hz, start_ms, dur_ms), ...])
SPECS = {
    # Bookends: an ascending fifth to open, the same fifth descending to
    # close. The sleep sound is the wake sound backwards, which needs no
    # learning; the wake chime keeps the old tick's 1175 Hz as its top note.
    "wake":  (3800, 0.32, [(784.0, 0, 200), (1174.7, 70, 310)]),
    "close": (2400, 0.32, [(1174.7, 0, 200), (784.0, 70, 340)]),
    # The count vocabulary - the count IS the message.
    "ok":    (9000, None, _seq([(660.0, 90)])),
    "busy":  (9000, None, _seq([(520.0, 70), (520.0, 70)])),
    "fail":  (9000, None, _seq([(330.0, 90), (330.0, 90), (330.0, 90)])),
    # Soft "still working" tick, repeats while an assistant answer is in
    # flight; and the rising pair = "news!" ahead of a background-task
    # announcement.
    "think":    (4500, None, _seq([(880.0, 40)])),
    "announce": (5000, None, _seq([(988.0, 50), (1319.0, 70)])),
}

_cache = {}


def set_gain(gain):
    """Global earcon volume (config voice.earconGain; 1.0 = as spec'd). Clears
    the cache so it takes effect even if something already played."""
    global GAIN
    gain = max(0.0, float(gain))
    if gain != GAIN:
        _cache.clear()
        GAIN = gain


def _note(freq, dur_ms, decay):
    """One note as a float array peaking near 1.0, silent at both ends."""
    n = int(SAMPLE_RATE * dur_ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    if decay is None:
        wave = np.sin(2 * np.pi * freq * t)
    else:
        tau = decay * dur_ms / 1000
        wave = np.zeros(n)
        for k, amp in PARTIALS:
            if freq * k < SAMPLE_RATE / 2:      # above Nyquist it aliases down
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
        mix = np.zeros(max(i + len(w) for i, w in zip(starts, waves)))
        for i, w in zip(starts, waves):
            mix[i:i + len(w)] += w
        # Normalize to the spec'd peak so an amplitude means the same thing
        # across earcons however many partials or overlapping notes built it.
        mix *= amp * GAIN / max(float(np.max(np.abs(mix))), 1e-9)
        _cache[name] = np.clip(mix, -32767, 32767).astype(np.int16)
    return _cache[name]


def pcm(name):
    """The earcon as raw s16le bytes for an output audio frame."""
    return samples(name).tobytes()
