"""Manual Ex-Link TV control (debug/bench tool):

    python exlink.py power_on|power_off|hdmi1|hdmi2|hdmi3|hdmi4

Frames and the COM port come from cglib/config.json - one home for both."""
import sys

import cglib


def main(argv):
    if len(argv) != 1 or argv[0] not in cglib.EXLINK_FRAMES:
        print("usage: exlink.py " + "|".join(cglib.EXLINK_FRAMES))
        return 2
    name = argv[0]
    port = cglib.load_config()["tvComPort"]
    ack = cglib.exlink_send(name, port)
    print(f"{name}: sent, response={ack or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
