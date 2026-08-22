"""Manual Ex-Link TV control:

    python exlink.py power_on|power_off|hdmi1..4|vol_up|vol_down|mute_toggle
    python exlink.py vol_set <0-100>

Frames and the COM port come from cglib/config.json.
"""
import sys

import cglib
import events


def _emit(cmd, ack=None, err=None, **extra):
    """Emit hand-run TV commands into the same stream as the launch lane's.

    lane=manual so operator probing does not skew launch-health metrics;
    event="exlink_send" still matches every frame sent by either lane."""
    if err is None:
        events.emit("manual", "exlink_send", cmd=cmd, ack=ack, **extra)
    else:
        events.emit("manual", "exlink_nak", events.ERROR, cmd=cmd, err=err,
                    **extra)


def main(argv):
    port = cglib.load_config()["tvComPort"]
    if len(argv) == 2 and argv[0] == "vol_set" and argv[1].isdigit():
        level = int(argv[1])
        if level > 100:
            print("vol_set takes 0-100")
            return 2
        frame = cglib.vol_set_frame(level)
        try:
            ack = cglib.exlink_send_hex(frame, port)
            _emit("vol_set", ack=ack, level_pct=level)
            print(f"vol_set {level}: sent {frame}, ack {ack}")
            return 0
        except cglib.ExlinkNak as e:
            _emit("vol_set", err=str(e), level_pct=level)
            print(f"vol_set {level}: FAILED - {e}")
            return 1
    if len(argv) == 1 and argv[0] in cglib.EXLINK_FRAMES:
        try:
            ack = cglib.exlink_send(argv[0], port)
            _emit(argv[0], ack=ack)
            print(f"{argv[0]}: sent, ack {ack}")
            return 0
        except cglib.ExlinkNak as e:
            _emit(argv[0], err=str(e))
            print(f"{argv[0]}: FAILED - {e}")
            return 1
    print("usage: exlink.py " + "|".join(cglib.EXLINK_FRAMES)
          + " | vol_set <0-100>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
