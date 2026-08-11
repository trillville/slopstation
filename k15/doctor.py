"""K15 chain diagnosis: python doctor.py

Read-only except one haptic chirp, and that only runs when the chord listener
is stopped (doctor detects it and skips - the one-process Puck rule is
enforced here, not by the operator). Run when the chord "does nothing", after
Windows/controller-firmware updates, or as a preflight.

Exit code = number of FAILs.
"""
import subprocess, sys, time

import cglib

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_counts = {PASS: 0, WARN: 0, FAIL: 0}


def report(level, name, detail, hint=""):
    _counts[level] += 1
    line = f"[{level}] {name}: {detail}"
    if hint and level != PASS:
        line += f"  -> {hint}"
    print(line, flush=True)


def check_config():
    required = ("gamingPcMac", "gamingPcIp", "sshHost", "tvComPort",
                "tvGamingCmd", "tvIdleCmd", "tvOffWhenDone")
    try:
        cfg = cglib.load_config()
    except Exception as e:
        report(FAIL, "config.json", f"unreadable ({e})", "recreate from k15/config.example.json")
        return None
    missing = [k for k in required if k not in cfg]
    if missing:
        report(FAIL, "config.json", f"missing keys: {missing}", "compare with k15/config.example.json")
    else:
        report(PASS, "config.json", f"{len(required)}/{len(required)} keys present")
    return cfg


def check_imports():
    ok = True
    for mod in ("serial", "hid"):
        try:
            __import__(mod)
            report(PASS, f"import {mod}", "ok")
        except Exception as e:
            ok = False
            report(FAIL, f"import {mod}", str(e),
                   "pip install pyserial hidapi (hidapi, NOT the 'hid' package)")
    return ok


def check_com(cfg):
    if not cfg:
        return
    try:
        import serial
        with serial.Serial(cfg["tvComPort"], 9600, timeout=1):
            pass
        report(PASS, "ex-link port", f"{cfg['tvComPort']} opens")
    except Exception as e:
        report(FAIL, "ex-link port", f"{cfg.get('tvComPort')}: {e}",
               "Device Manager > Ports; SH-U35B unplugged or COM number changed?")


def check_puck():
    try:
        import hid
        n = len(hid.enumerate(cglib.VID, cglib.PID))
    except Exception as e:
        report(FAIL, "puck enumerate", str(e), "hidapi broken?")
        return False
    if n:
        report(PASS, "puck", f"{n} HID interfaces enumerated")
        return True
    report(FAIL, "puck", "no interfaces for VID 28DE PID 1304",
           "Puck unplugged from the K15, or claimed weirdly - check USB + VirtualHere server")
    return False


def check_listener():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
             "| Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=20)
        running = "chord_listener" in (r.stdout or "")
    except Exception as e:
        report(WARN, "listener", f"could not detect ({e})", "assuming not running")
        return False
    if running:
        report(PASS, "listener", "running (owns the Puck - haptic check skipped)")
    else:
        report(WARN, "listener", "NOT running - the chord is deaf",
               "run Start-Listener.bat (or it's mid-restart; re-check in 10s)")
    return running


def check_haptics():
    """Only called when the listener is stopped. Needs the controller awake."""
    try:
        from haptic_test import open_input_interface, chirp
        dev = open_input_interface()
    except Exception as e:
        report(WARN, "haptics", str(e),
               "controller asleep? tap a button and rerun; or a session is active")
        return
    try:
        chirp(dev, 0)
        report(PASS, "haptics", "chirp sent - you should have felt it (if not: rerun after firmware calibrate)")
    except Exception as e:
        report(FAIL, "haptics", f"write failed ({e})",
               "protocol drift after firmware update? re-run calibrate.py + haptic_test.py")
    finally:
        dev.close()


def check_ssh():
    try:
        from couch import ssh
    except Exception as e:
        report(FAIL, "ssh", f"couch.py unimportable ({e})", "config broken? see above")
        return
    try:
        st = ssh("status")
        report(PASS, "ssh status", f"-> {st!r} (key, forced command, sshd, firewall all good)")
    except subprocess.TimeoutExpired:
        report(WARN, "ssh status", "timed out", "PC asleep? that's normal from idle; wake it to fully test")
    except Exception as e:
        report(FAIL, "ssh status", str(e),
               "PC awake? then check sshd service / firewall rule / administrators_authorized_keys")
        return
    try:
        ssh("bogus")
        report(WARN, "ssh dispatch", "bogus command did NOT get DENIED", "Dispatch.ps1 changed?")
    except subprocess.CalledProcessError as e:
        if "DENIED" in (e.stdout or ""):
            report(PASS, "ssh dispatch", "unknown verbs DENIED")
        else:
            report(WARN, "ssh dispatch", f"unexpected reply {e.stdout!r}", "check Dispatch.ps1")
    except Exception as e:
        report(WARN, "ssh dispatch", str(e), "transient? status check above is the primary signal")


def check_session_state():
    lock = cglib.BASE / "state" / "session.lock"
    try:
        age = time.time() - lock.stat().st_mtime
        import couch
        if age < couch.LOCK_STALE_S:
            report(PASS, "session lock", f"fresh ({age:.0f}s) - a session/launch is active")
        else:
            report(WARN, "session lock", f"stale ({age:.0f}s)",
                   "harmless - next launch or reconcile recycles it")
    except OSError:
        report(PASS, "session lock", "none (idle)")
    err = cglib.BASE / "state" / "last_error"
    try:
        report(WARN, "last_error", err.read_text().strip() or "(empty)",
               "most recent launch failure - see couch.log")
    except OSError:
        report(PASS, "last_error", "none")


if __name__ == "__main__":
    cfg = check_config()
    check_imports()
    check_com(cfg)
    puck_ok = check_puck()
    listener_running = check_listener()
    if puck_ok and not listener_running:
        check_haptics()
    check_ssh()
    check_session_state()
    print(f"\n{_counts[PASS]} pass, {_counts[WARN]} warn, {_counts[FAIL]} fail")
    sys.exit(_counts[FAIL])
