"""Rediscover the controller's HID button bytes: python -m slopstation.calibrate

Valve does not document the 2026 Steam Controller's report format, so
chord_listener.py's RID_INPUT / BTN_BYTE / CHORD are measured, not a contract.
Re-run after a controller firmware update; a shuffled layout shows up as a
listener that prints `armed` and never fires.

Controller awake and flat for the 3 s noise window, then press ONE input at a
time. Not while chord_listener.py is running - one process owns the Puck.
"""

import time
from collections import Counter

from slopstation import haptics


def pick_interface():
    # Report type unknown here - that is what this tool measures - so accept on
    # volume of traffic, not on the report id.
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
