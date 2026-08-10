import time
import hid

from cglib import VID, PID

def pick_interface():
    for d in hid.enumerate(VID, PID):
        try:
            h = hid.device(); h.open_path(d["path"]); h.set_nonblocking(True)
            t0 = time.time(); got = 0
            while time.time() - t0 < 2.0:
                if h.read(64): got += 1
                time.sleep(0.005)
            if got > 10:
                print(f"using interface: {d['path']}")
                return h
            h.close()
        except (OSError, ValueError):
            pass
    return None

dev = pick_interface()
if not dev:
    print("no streaming interface found"); raise SystemExit(1)

print("learning noise for 3s - hands OFF, controller flat...")
reports = []
t0 = time.time()
while time.time() - t0 < 3.0:
    r = dev.read(64)
    if r: reports.append(bytes(r))
    time.sleep(0.002)

# group by report type (byte 0), keep the most common type only
from collections import Counter
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
            diffs = [f"byte[{i}]: {base[i]:02x}->{r[i]:02x}" for i in stable if r[i] != base[i]]
            if diffs:
                print("  ".join(diffs))
            last = r
    time.sleep(0.002)