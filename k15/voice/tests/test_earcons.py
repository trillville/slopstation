"""Blind test (C1 s1): earcon synthesis invariants - burst counts carry the
vocabulary, so segmentation must recover exactly the spec'd count; fades must
kill clicks; amplitudes must be sane; the wake/close bells must stay quieter
than the count vocabulary (that is the whole point of the bell voice); the
gain knob must scale and never wrap int16. Run:
    .venv\\Scripts\\python tests\\test_earcons.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earcons


def burst_count(x, silence=100, block_ms=2):
    """Count contiguous non-silent runs on a block-max envelope (raw samples
    dip through zero every half sine period; the envelope doesn't)."""
    block = int(earcons.SAMPLE_RATE * block_ms / 1000)
    n = len(x) // block * block
    env = np.abs(x[:n].astype(np.int32)).reshape(-1, block).max(axis=1)
    loud = env > silence
    return int(np.count_nonzero(np.diff(loud.astype(np.int8)) == 1) + loud[0])


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def test_specs():
    expected_counts = {"wake": 1, "ok": 1, "busy": 2, "fail": 3, "close": 1,
                       "think": 1, "announce": 2}
    assert set(expected_counts) == set(earcons.SPECS)

    for name, want in expected_counts.items():
        x = earcons.samples(name)
        amp, _, notes = earcons.SPECS[name]
        # Length: the last note's end, exact sample math (bells overlap, the
        # counted earcons don't - both fall out of the same layout).
        n_expected = max(int(earcons.SAMPLE_RATE * start / 1000)
                         + int(earcons.SAMPLE_RATE * dur / 1000)
                         for _, start, dur in notes)
        assert len(x) == n_expected, f"{name}: {len(x)} != {n_expected}"
        # The count IS the message: a bell's decay tail must not bridge the
        # gap of a counted earcon, and its two notes must not read as two.
        got = burst_count(x)
        assert got == want, f"{name}: segmented {got} bursts, want {want}"
        # Fade: edges quiet (no clicks), peak lands on the spec'd amplitude.
        assert abs(int(x[0])) < 200 and abs(int(x[-1])) < 200, f"{name}: clicky edges"
        peak = int(np.max(np.abs(x.astype(np.int32))))
        assert amp * 0.9 <= peak <= amp * 1.01, f"{name}: peak {peak} vs amp {amp}"
        # PCM bytes are s16le of the same array.
        assert earcons.pcm(name) == x.tobytes()
    print(f"OK - {len(expected_counts)} earcons: counts, lengths, fades, amplitudes")


def test_bookends_are_the_quiet_ones():
    """The wake chime fires on every wake, so it must be audibly softer than
    an action ack - and RMS, not peak, is what the ear reports. Sleep softer
    still: it is information, not an answer."""
    loudest_ack = max(rms(earcons.samples(n)) for n in ("ok", "busy", "fail"))
    wake, close = rms(earcons.samples("wake")), rms(earcons.samples("close"))
    assert wake < loudest_ack / 2, f"wake rms {wake:.0f} vs ack {loudest_ack:.0f}"
    assert close < wake, f"close rms {close:.0f} not under wake {wake:.0f}"
    print(f"OK - bookends quieter than the acks (wake {wake:.0f}, "
          f"close {close:.0f}, loudest ack {loudest_ack:.0f} rms)")


def test_gain_knob():
    """config voice.earconGain retunes volume without touching code - and a
    silly value must clip, not wrap: int16 overflow turns a chime into a
    buzzsaw at full scale."""
    base = earcons.samples("wake").copy()
    try:
        earcons.set_gain(0.5)
        half = earcons.samples("wake")
        assert abs(np.max(np.abs(half.astype(np.int32)))
                   - np.max(np.abs(base.astype(np.int32))) / 2) <= 2, "gain did not halve"
        earcons.set_gain(100.0)
        loud = earcons.samples("ok").astype(np.int32)
        assert np.max(np.abs(loud)) <= 32767, "gain wrapped int16"
        assert np.max(loud) > 32000 and np.min(loud) < -32000, "clip did not clip"
    finally:
        earcons.set_gain(1.0)
    assert np.array_equal(earcons.samples("wake"), base), "gain reset must restore"
    print("OK - gain: scales, clips at full scale, cache follows the knob")


def main():
    test_specs()
    test_bookends_are_the_quiet_ones()
    test_gain_knob()


if __name__ == "__main__":
    main()
