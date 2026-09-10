"""Test the Tv device: Ex-Link frames, the COM retry, ack validation, status."""

import sys
import time
import types

import pytest

from slopstation import tv

FRAME = tv.EXLINK_FRAMES["power_on"]  # any valid frame; the port never looks at it


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


def test_every_frame_is_seven_bytes_and_its_checksum_zeroes_the_sum():
    # The worksheet's own example: volume 20 -> checksum 0xC1.
    assert tv.exlink_frame(0x01, 0x00, 0x00, 20) == "082201000014c1"
    for name, hexs in tv.EXLINK_FRAMES.items():
        b = bytes.fromhex(hexs)
        assert len(b) == 7, f"{name}: {len(b)} bytes"
        assert (sum(b) & 0xFF) == 0, f"{name}: checksum does not zero the sum"
    assert tv.INPUTS == ("hdmi1", "hdmi2", "hdmi3", "hdmi4")


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


def test_status_is_every_read_at_once_with_none_for_unknown(monkeypatch):
    monkeypatch.setattr(tv, "tv_power_state", lambda ip, **kw: "on")
    monkeypatch.setattr(tv, "tv_volume", lambda ip, **kw: 14)
    monkeypatch.setattr(tv, "_read_rendering", lambda ip, *a, **kw: None)
    device = tv.Tv({"tvIp": "tv"}, lambda *a, **kw: None)
    assert device.status() == {"power": "on", "volume": 14, "muted": None}
    assert tv.Tv({}, None).status() == {"power": None, "volume": None, "muted": None}
