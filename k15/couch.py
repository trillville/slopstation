import json, pathlib, socket, subprocess, sys, time
import serial

BASE = pathlib.Path(__file__).parent
CFG  = json.loads((BASE / "config.json").read_text())
LOCK = BASE / "state" / "session.lock"
LOGF = BASE / "couch.log"

EXLINK = {"power_on": "082200000002d4", "power_off": "082200000001d5",
          "hdmi1": "08220a000500c7", "hdmi2": "08220a000501c6",
          "hdmi3": "08220a000502c5", "hdmi4": "08220a000503c4"}

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOGF.open("a", encoding="utf-8") as f: f.write(line + "\n")
    except OSError: pass

def exlink(name):
    try:
        with serial.Serial(CFG["tvComPort"], 9600, timeout=1) as s:
            s.write(bytes.fromhex(EXLINK[name]))
            log(f"exlink {name} -> {s.read(3).hex() or 'no-ack'}")
    except Exception as e:
        log(f"exlink {name} FAILED: {e}")   # non-fatal: PC readiness is independent

def wol():
    mac = bytes.fromhex(CFG["gamingPcMac"].replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
    log("WOL sent")

def ssh(cmd, timeout=15):
    r = subprocess.run(["ssh", CFG["sshHost"], cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()

def wait_port(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((CFG["gamingPcIp"], 22), 3):
                return True
        except OSError:
            time.sleep(1)
    return False

def start():
    if LOCK.exists():
        log("session already active/starting - ignoring"); return 1
    LOCK.parent.mkdir(exist_ok=True); LOCK.write_text(str(time.time()))
    try:
        log("=== LAUNCH ===")
        exlink("power_on")
        wol()
        if not wait_port(): raise RuntimeError("gaming PC never became reachable")
        log("ssh port up")
        for _ in range(60):
            try:
                if ssh("enter") == "OK":
                    log("enter dispatched"); break
            except Exception as e:
                log(f"enter attempt failed ({e}) - retrying")
            time.sleep(1)
        else: raise RuntimeError("could not trigger Enter task")
        end = time.time() + 120
        ready = False
        while time.time() < end:
            try:
                st = ssh("status")
                if st != "NOTREADY":
                    log(f"host READY ({st})"); ready = True; break
            except Exception as e:
                log(f"status poll failed ({e}) - retrying")
            time.sleep(1)
        if not ready: raise RuntimeError("host never reported READY")
        exlink(CFG["tvGamingCmd"])
        log("=== GAMING ==="); watch()
    except Exception as e:
        log(f"launch failed: {e} - TV input untouched")
        LOCK.unlink(missing_ok=True); return 1
    return 0

def watch():
    fails = 0
    while True:
        time.sleep(5)
        try:
            st = ssh("status"); fails = 0
            if st == "NOTREADY":
                log("host reports session ended"); break
        except Exception:
            fails += 1
            if fails >= 3:
                log("gaming PC gone (slept/crashed) - treating as ended"); break
    exlink("power_off" if CFG["tvOffWhenDone"] else CFG["tvIdleCmd"])
    LOCK.unlink(missing_ok=True)
    log("=== IDLE ===")

if __name__ == "__main__":
    sys.exit(start() if (len(sys.argv) < 2 or sys.argv[1] == "start") else 0)