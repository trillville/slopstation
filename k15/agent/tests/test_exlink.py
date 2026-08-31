"""Blind test: the frozen EXLINK_FRAMES literals, the checksum builder, and
vol_set clamping must agree - every table entry is rebuilt from its
(c1, c2, c3, value) spec. Run:
    .venv\\Scripts\\python tests\\test_exlink.py
"""
import sys
import time

import _bootstrap  # noqa: F401
import tv

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
    assert set(SPECS) == set(tv.EXLINK_FRAMES), (
        f"table/spec drift: {set(SPECS) ^ set(tv.EXLINK_FRAMES)}")
    for name, spec in SPECS.items():
        built = tv.exlink_frame(*spec)
        frozen = tv.EXLINK_FRAMES[name]
        assert built == frozen, f"{name}: builder={built} literal={frozen}"

    # Worksheet's own example: volume 20 -> checksum 0xC1.
    assert tv.vol_set_frame(20) == "082201000014c1"
    assert tv.vol_set_frame(0) == tv.exlink_frame(0x01, 0x00, 0x00, 0)
    assert tv.vol_set_frame(-5) == tv.vol_set_frame(0)      # clamp low
    assert tv.vol_set_frame(250) == tv.vol_set_frame(100)   # clamp high

    # COM contention: retry once after a settle, then propagate.
    import types
    calls = {"n": 0}

    class FakePort:
        def __init__(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise fake_serial.SerialException("busy")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, b): pass
        def read(self, n): return bytes.fromhex("030cf1")

    fake_serial = types.ModuleType("serial")
    fake_serial.SerialException = type("SerialException", (Exception,), {})
    fake_serial.Serial = FakePort
    sys.modules["serial"] = fake_serial
    _real_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        assert tv.exlink_send_hex("082202000000d4", "COMX") == "030cf1"
        assert calls["n"] == 2, "should have retried once"
        calls["n"] = 0
        FakePort.__init__ = lambda self, *a, **k: (_ for _ in ()).throw(
            fake_serial.SerialException("always"))
        try:
            tv.exlink_send_hex("082202000000d4", "COMX")
            assert False, "second failure must propagate"
        except fake_serial.SerialException:
            pass

        # --- ack validation: 030cf1 or the command didn't land
        FakePort.__init__ = lambda self, *a, **k: None

        FakePort.read = lambda self, n: bytes.fromhex("030cff")   # NAK
        try:
            tv.exlink_send_hex("082202000000d4", "COMX")
            assert False, "NAK must raise ExlinkNak"
        except tv.ExlinkNak:
            pass

        FakePort.read = lambda self, n: b""                       # TV silent/off
        try:
            tv.exlink_send_hex("082202000000d4", "COMX")
            assert False, "missing ack must raise ExlinkNak"
        except tv.ExlinkNak:
            pass
    finally:
        time.sleep = _real_sleep
        del sys.modules["serial"]

    for name, hexs in tv.EXLINK_FRAMES.items():
        b = bytes.fromhex(hexs)
        assert len(b) == 7, f"{name}: {len(b)} bytes"
        assert (sum(b) & 0xFF) == 0, f"{name}: checksum does not zero the sum"

    print(f"OK - {len(SPECS)} frames cross-checked, vol_set clamps, "
          f"ack validation raises on NAK/silence")


if __name__ == "__main__":
    main()
