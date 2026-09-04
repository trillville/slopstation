"""Measure the controller's HID report and button bytes.

Stop chord_listener.py, wake the controller, and keep it still during the
three-second baseline. Re-run after controller firmware updates.
"""

import time
from collections import Counter

from slopstation import haptics


def pick_interface():
    # Select by traffic volume because the report type is not known yet.
    dev, path = haptics.open_streaming_interface(lambda reads: len(reads) > 10)
    if dev:
        print(f"using interface: {path}")
    return dev


def main():
    dev = pick_interface()
    if not dev:
        print("no streaming interface found")
        raise SystemExit(1)

    print("learning noise for 3s - hands OFF, controller flat...")
    reports = []
    t0 = time.time()
    while time.time() - t0 < 3.0:
        r = dev.read(64)
        if r:
            reports.append(bytes(r))
        time.sleep(0.002)

    # group by report type (byte 0), keep the most common type only
    rid = Counter(r[0] for r in reports).most_common(1)[0][0]
    reports = [r for r in reports if r[0] == rid]
    n = min(len(r) for r in reports)
    base = reports[-1]

    # any byte that varied during the quiet window is noise - ignore it
    noisy = {i for i in range(n) for r in reports if r[i] != base[i]}
    stable = [i for i in range(n) if i not in noisy]
    print(f"report type: {rid:02x}   ignoring noisy bytes: {sorted(noisy)}")
    print(f"watching stable bytes: {stable}")
    print("press ONE input at a time (Steam, then right grip). Ctrl+C to quit.\n")

    last = base
    while True:
        r = dev.read(64)
        if r:
            r = bytes(r)
            if len(r) >= n and r[0] == rid and r != last:
                diffs = [
                    f"byte[{i}]: {base[i]:02x}->{r[i]:02x}"
                    for i in stable
                    if r[i] != base[i]
                ]
                if diffs:
                    print("  ".join(diffs))
                last = r
        time.sleep(0.002)


if __name__ == "__main__":
    main()
