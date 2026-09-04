"""Test Ex-Link frames, checksums, and volume clamping."""

import sys
import time
import types

import pytest

from slopstation import tv

# name -> (c1, c2, c3, value), straight from the official worksheet rows.
SPECS = {
    "power_on": (0x00, 0x00, 0x00, 0x02),
    "power_off": (0x00, 0x00, 0x00, 0x01),
    "hdmi1": (0x0A, 0x00, 0x05, 0x00),
    "hdmi2": (0x0A, 0x00, 0x05, 0x01),
    "hdmi3": (0x0A, 0x00, 0x05, 0x02),
    "hdmi4": (0x0A, 0x00, 0x05, 0x03),
    "vol_up": (0x01, 0x00, 0x01, 0x00),
    "vol_down": (0x01, 0x00, 0x02, 0x00),
    "mute_toggle": (0x02, 0x00, 0x00, 0x00),
}

FRAME = "082202000000d4"  # any valid frame; the port never looks at it


@pytest.fixture
def port():
    """The one COM port's script: `busy` opens raise SerialException before
    one succeeds (None = every one), `answer` is the hex the TV reads back,
    `opens` counts the attempts."""
    return {"busy": 0, "answer": "030cf1", "opens": 0}


@pytest.fixture
def fake_serial(port, monkeypatch):
    """A `serial` module for tv's lazy import, whose Serial follows `port`.
    The contention settle does not sleep."""
    fake = types.ModuleType("serial")
    fake.SerialException = type("SerialException", (Exception,), {})

    class FakePort:
        def __init__(self, *a, **k):
            port["opens"] += 1
            if port["busy"] is None or port["opens"] <= port["busy"]:
                raise fake.SerialException("busy")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            pass

        def read(self, n):
            return bytes.fromhex(port["answer"])

    fake.Serial = FakePort
    monkeypatch.setitem(sys.modules, "serial", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return fake


def test_every_frozen_frame_rebuilds_from_its_spec():
    assert set(SPECS) == set(tv.EXLINK_FRAMES), (
        f"table/spec drift: {set(SPECS) ^ set(tv.EXLINK_FRAMES)}"
    )
    for name, spec in SPECS.items():
        built = tv.exlink_frame(*spec)
        frozen = tv.EXLINK_FRAMES[name]
        assert built == frozen, f"{name}: builder={built} literal={frozen}"


def test_vol_set_frame_checksum_and_clamping():
    # Worksheet's own example: volume 20 -> checksum 0xC1.
    assert tv.vol_set_frame(20) == "082201000014c1"
    assert tv.vol_set_frame(0) == tv.exlink_frame(0x01, 0x00, 0x00, 0)
    assert tv.vol_set_frame(-5) == tv.vol_set_frame(0)  # clamp low
    assert tv.vol_set_frame(250) == tv.vol_set_frame(100)  # clamp high


def test_com_contention_retries_once_after_a_settle_then_propagates(fake_serial, port):
    port["busy"] = 1
    assert tv.exlink_send_hex(FRAME, "COMX") == "030cf1"
    assert port["opens"] == 2, "should have retried once"
    port["opens"], port["busy"] = 0, None
    with pytest.raises(fake_serial.SerialException):
        tv.exlink_send_hex(FRAME, "COMX")  # second failure must propagate


def test_ack_validation_030cf1_or_the_command_did_not_land(fake_serial, port):
    port["answer"] = "030cff"  # NAK
    with pytest.raises(tv.ExlinkNak):
        tv.exlink_send_hex(FRAME, "COMX")
    port["answer"] = ""  # TV silent/off
    with pytest.raises(tv.ExlinkNak):
        tv.exlink_send_hex(FRAME, "COMX")


def test_every_frame_is_seven_bytes_and_its_checksum_zeroes_the_sum():
    for name, hexs in tv.EXLINK_FRAMES.items():
        b = bytes.fromhex(hexs)
        assert len(b) == 7, f"{name}: {len(b)} bytes"
        assert (sum(b) & 0xFF) == 0, f"{name}: checksum does not zero the sum"
