import sys, serial

PORT = "COM3"  # <- your COM port
CMDS = {
    "power_on":  "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
}
# Frame: 08 22 c1 c2 c3 value + checksum, checksum = (0x100 - sum(first 6)) & 0xFF

def send(name):
    with serial.Serial(PORT, 9600, timeout=1) as s:
        s.write(bytes.fromhex(CMDS[name]))
        resp = s.read(3)
    print(f"{name}: sent, response={resp.hex() or '(none)'}")
    return resp

if __name__ == "__main__":
    send(sys.argv[1])