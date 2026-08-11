"""Manual Ex-Link TV control (debug/bench tool):

    python exlink.py power_on|power_off|hdmi1..4|vol_up|vol_down|mute_toggle
    python exlink.py vol_set <0-100>
    python exlink.py probe_volume     (bench: raw answer to the status query -
                                       decided 2026-08-10: canned echo, no state)
    python exlink.py decode_volume    (audible: probe at known states so any
                                       volume/mute bytes would identify themselves)

Frames and the COM port come from cglib/config.json - one home for both."""
import sys
import time

import cglib


def _probe(port, label):
    resp = cglib.exlink_probe(cglib.EXLINK_VOLUME_QUERY, port)
    print(f"{label}: {resp or '(none)'}")
    return resp


def _diff(a_label, a, b_label, b):
    if not a or not b:
        print(f"diff {a_label} vs {b_label}: skipped (empty response)")
        return
    if len(a) != len(b):
        print(f"diff {a_label} vs {b_label}: lengths differ "
              f"({len(a) // 2}B vs {len(b) // 2}B)")
        return
    ba, bb = bytes.fromhex(a), bytes.fromhex(b)
    hits = [i for i in range(len(ba)) if ba[i] != bb[i]]
    if hits:
        print(f"diff {a_label} vs {b_label}: "
              + ", ".join(f"byte[{i}] {ba[i]:02x}->{bb[i]:02x}" for i in hits))
    else:
        print(f"diff {a_label} vs {b_label}: identical")


def decode_volume(port):
    """Differential decode (audible - volume will move): probe the query at
    KNOWN states so the value bytes identify themselves. Whatever byte tracks
    vol_set is the volume; whatever flips with mute_toggle is the mute flag.
    Leaves the TV at volume 23 with mute back where it started."""
    try:
        _probe(port, "baseline         ")
        time.sleep(0.4)
        cglib.exlink_send_hex(cglib.vol_set_frame(7), port)
        time.sleep(0.4)
        v7 = _probe(port, "after vol_set 7  ")
        time.sleep(0.4)
        cglib.exlink_send_hex(cglib.vol_set_frame(23), port)
        time.sleep(0.4)
        v23 = _probe(port, "after vol_set 23 ")
        time.sleep(0.4)
        cglib.exlink_send("mute_toggle", port)
        time.sleep(0.4)
        muted = _probe(port, "after mute_toggle")
        time.sleep(0.4)
        cglib.exlink_send("mute_toggle", port)
        time.sleep(0.4)
        restored = _probe(port, "after mute back  ")
    except cglib.ExlinkNak as e:
        print(f"decode aborted - {e}")
        return 1
    _diff("vol=7", v7, "vol=23", v23)
    _diff("unmuted", v23, "muted", muted)
    _diff("mute-restored", restored, "vol=23", v23)
    print("TV left at volume 23, mute restored. Paste ALL of the above.")
    return 0


def main(argv):
    port = cglib.load_config()["tvComPort"]
    if len(argv) == 2 and argv[0] == "vol_set" and argv[1].isdigit():
        level = int(argv[1])
        if level > 100:
            print("vol_set takes 0-100")
            return 2
        frame = cglib.vol_set_frame(level)
        try:
            cglib.exlink_send_hex(frame, port)
            print(f"vol_set {level}: sent {frame}, ack {cglib.EXLINK_ACK}")
            return 0
        except cglib.ExlinkNak as e:
            print(f"vol_set {level}: FAILED - {e}")
            return 1
    if len(argv) == 1 and argv[0] == "probe_volume":
        # Generous 16-byte read: a payload after the ack can't hide behind
        # exlink_send_hex's read(3). The answer decides mute-state design.
        resp = cglib.exlink_probe(cglib.EXLINK_VOLUME_QUERY, port)
        if not resp:
            print("volume query: no response - set is write-only; "
                  "software mute state it is")
        elif resp == cglib.EXLINK_ACK:
            print(f"volume query: bare ack {resp}, no payload - the set "
                  "accepts the frame but answers no data; blind mute stays")
        else:
            print(f"volume query: response={resp} - more than the ack; run "
                  "decode_volume to check it actually varies with state "
                  "(S90C verdict 2026-08-10: constant canned echo, no state)")
        return 0
    if len(argv) == 1 and argv[0] == "decode_volume":
        return decode_volume(port)
    if len(argv) == 1 and argv[0] in cglib.EXLINK_FRAMES:
        try:
            cglib.exlink_send(argv[0], port)
            print(f"{argv[0]}: sent, ack {cglib.EXLINK_ACK}")
            return 0
        except cglib.ExlinkNak as e:
            print(f"{argv[0]}: FAILED - {e}")
            return 1
    print("usage: exlink.py " + "|".join(cglib.EXLINK_FRAMES)
          + " | vol_set <0-100> | probe_volume | decode_volume")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
