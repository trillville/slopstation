"""Earcon synthesis: the count vocabulary as audio, plus the two session
bookends (a wake chime and its mirror at sleep) and the background lane's
announcement cue. Synthesized at import from specs; no binary assets in the
repo. All PCM is SAMPLE_RATE mono s16le, ready to wrap in an
OutputAudioRawFrame.

The counts are the contract, mirroring the haptic thuds: 1 = accepted,
2 = busy, 3 = failed. Pitch, contour and level are taste, tuned so the six
sound like one family - the bookends quietest, the acks just above them, and
the announcement cue on top, since it is the only one that has to carry across
the room unasked.

Everything is a bell: overlapping or sequential notes, a few light partials,
exponential decay. A decaying note is perceptually far softer than a flat one
of the same peak - flat sines at tick level are exactly what made the old
vocabulary jarring - so these read much quieter than their amplitudes suggest.
The engine still does flat tones (decay=None) if a spec ever wants one.

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
    # The count vocabulary - the count IS the message (it mirrors the haptic
    # thuds), so contour and timbre are free to make each one distinctive:
    #   ok   - one bell on D6, the note the wake chime landed on: "yes, that"
    #   busy - the same note struck twice, flat, like a knock at a shut door
    #   fail - three falling, the shape every ear already reads as "no"
    # Levels sit just above the wake chime, not 6x over it: ok fires on every
    # single command, so it has to be an answer, not an alarm.
    "ok":    (5200, 0.30, _seq([(1174.7, 260)])),
    "busy":  (4600, 0.26, _seq([(880.0, 150), (880.0, 150)])),
    "fail":  (4600, 0.26, _seq([(698.5, 160), (587.3, 160), (440.0, 160)], 60)),
    # The announcement cue is the loudest: it arrives unasked, across the
    # room, ahead of spoken news, and a rising third is the one shape here
    # that sounds like a question being opened rather than an answer closing.
    "announce": (6200, 0.30, _seq([(987.8, 200), (1318.5, 300)])),
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
