"""Blind test (C1 s4): the frozen EXLINK_FRAMES literals, the checksum
builder, and vol_set clamping must all agree. Rebuilds every table entry from
its (c1, c2, c3, value) spec - if a literal was ever hand-typed wrong, or the
builder's checksum math drifts, this fails. Run:
    .venv\\Scripts\\python tests\\test_exlink.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cglib

# name -> (c1, c2, c3, value), straight from the official worksheet rows.
SPECS = {
    "power_on":    (0x00, 0x00, 0x00, 0x02),
    "power_off":   (0x00, 0x00, 0x00, 0x01),
    "hdmi1":       (0x0A, 0x00, 0x05, 0x00),
    "hdmi2":       (0x0A, 0x00, 0x05, 0x01),
    "hdmi3":       (0x0A, 0x00, 0x05, 0x02),
    "hdmi4":       (0x0A, 0x00, 0x05, 0x03),
    "vol_up":      (0x01, 0x00, 0x01, 0x00),
    "vol_down":    (0x01, 0x00, 0x02, 0x00),
    "mute_toggle": (0x02, 0x00, 0x00, 0x00),
}


def main():
    assert set(SPECS) == set(cglib.EXLINK_FRAMES), (
        f"table/spec drift: {set(SPECS) ^ set(cglib.EXLINK_FRAMES)}")
    for name, spec in SPECS.items():
        built = cglib.exlink_frame(*spec)
        frozen = cglib.EXLINK_FRAMES[name]
        assert built == frozen, f"{name}: builder={built} literal={frozen}"

    # Worksheet's own example: volume 20 -> checksum 0xC1.
    assert cglib.vol_set_frame(20) == "082201000014c1"
    assert cglib.vol_set_frame(0) == cglib.exlink_frame(0x01, 0x00, 0x00, 0)
    assert cglib.vol_set_frame(-5) == cglib.vol_set_frame(0)      # clamp low
    assert cglib.vol_set_frame(250) == cglib.vol_set_frame(100)   # clamp high

    # The bench query probe frame is also builder-consistent.
    assert cglib.EXLINK_VOLUME_QUERY == cglib.exlink_frame(0xF0, 0x01, 0x00, 0x00)

    # Every frame is 7 bytes and its own checksum verifies.
    for name, hexs in {**cglib.EXLINK_FRAMES,
                       "volume_query": cglib.EXLINK_VOLUME_QUERY}.items():
        b = bytes.fromhex(hexs)
        assert len(b) == 7, f"{name}: {len(b)} bytes"
        assert (sum(b) & 0xFF) == 0, f"{name}: checksum does not zero the sum"

    print(f"OK - {len(SPECS)} frames cross-checked, vol_set clamps, query frame verified")


if __name__ == "__main__":
    main()
