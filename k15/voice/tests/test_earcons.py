"""Blind test (C1 s1): earcon synthesis invariants - burst counts carry the
vocabulary, so segmentation must recover exactly the spec'd count; fades must
kill clicks; amplitudes must be sane. Run:
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


def main():
    expected_counts = {"wake": 1, "ok": 1, "busy": 2, "fail": 3, "close": 1}
    assert set(expected_counts) == set(earcons.SPECS)

    for name, want in expected_counts.items():
        x = earcons.samples(name)
        amp, bursts = earcons.SPECS[name]
        # Length: bursts + inter-burst gaps, exact sample math.
        n_expected = sum(int(earcons.SAMPLE_RATE * d / 1000) for _, d in bursts)
        n_expected += (len(bursts) - 1) * int(earcons.SAMPLE_RATE * earcons.GAP_MS / 1000)
        assert len(x) == n_expected, f"{name}: {len(x)} != {n_expected}"
        # The count IS the message.
        got = burst_count(x)
        assert got == want, f"{name}: segmented {got} bursts, want {want}"
        # Fade: edges quiet (no clicks), peak near spec amplitude.
        assert abs(int(x[0])) < 200 and abs(int(x[-1])) < 200, f"{name}: clicky edges"
        peak = int(np.max(np.abs(x.astype(np.int32))))
        assert amp * 0.9 <= peak <= amp * 1.01, f"{name}: peak {peak} vs amp {amp}"
        # PCM bytes are s16le of the same array.
        assert earcons.pcm(name) == x.tobytes()

    print(f"OK - {len(expected_counts)} earcons: counts, lengths, fades, amplitudes")


if __name__ == "__main__":
    main()
