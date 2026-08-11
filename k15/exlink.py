"""Manual Ex-Link TV control (debug/bench tool):

    python exlink.py power_on|power_off|hdmi1..4|vol_up|vol_down|mute_toggle
    python exlink.py vol_set <0-100>
    python exlink.py probe_volume     (C1 drill: does this set answer queries?)

Frames and the COM port come from cglib/config.json - one home for both."""
import sys

import cglib


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
            print(f"volume query: response={resp} - MORE THAN THE ACK. "
                  "Paste this: real volume/mute state may be readable")
        return 0
    if len(argv) == 1 and argv[0] in cglib.EXLINK_FRAMES:
        try:
            cglib.exlink_send(argv[0], port)
            print(f"{argv[0]}: sent, ack {cglib.EXLINK_ACK}")
            return 0
        except cglib.ExlinkNak as e:
            print(f"{argv[0]}: FAILED - {e}")
            return 1
    print("usage: exlink.py " + "|".join(cglib.EXLINK_FRAMES)
          + " | vol_set <0-100> | probe_volume")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
