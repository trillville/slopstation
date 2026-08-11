"""K15 chain diagnosis: python doctor.py

Read-only except one haptic chirp, and that only runs when the chord listener
is stopped (doctor detects it and skips - the one-process Puck rule is
enforced here, not by the operator). Run when the chord "does nothing", after
Windows/controller-firmware updates, or as a preflight.

Voice rows at the end are WARN-only: voice is an overlay, never load-bearing,
so its problems must not turn the chain diagnosis red.

Exit code = number of FAILs.
"""
import json, subprocess, sys, time

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


def _python_cmdlines():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
         "| Select-Object -ExpandProperty CommandLine"],
        capture_output=True, text=True, timeout=20)
    return r.stdout or ""


def check_listener():
    try:
        running = "chord_listener" in _python_cmdlines()
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


def check_voice(cfg):
    """Voice overlay health - WARN-only by design: voice is never load-bearing,
    so a broken voice lane must not turn the chain doctor red (exit code =
    FAILs, and the chord's chain is what that number means). Filesystem +
    process checks only - doctor runs on system python, which deliberately
    does not have the voice venv's deps."""
    voice_dir = cglib.BASE / "voice"
    if not (cfg and isinstance(cfg.get("voice"), dict)):
        report(WARN, "voice config", "no voice section in config.json",
               "copy the voice block from config.example.json to enable voice")
        return
    try:
        secrets = cglib.load_secrets()
    except Exception as e:
        report(WARN, "voice secrets", f"unreadable ({e})",
               "recreate from secrets.template.json")
        secrets = {}
    lanes = {"deepgramApiKey": "STT+TTS", "anthropicApiKey": "assistant",
             "openaiApiKey": "assistant A/B", "steamApiKey": "library owned/meta"}
    live = [what for key, what in lanes.items() if cglib.real_key(secrets.get(key))]
    dead = [what for key, what in lanes.items() if not cglib.real_key(secrets.get(key))]
    report(PASS if "STT+TTS" in live else WARN, "voice keys",
           f"live: {', '.join(live) or 'none'}"
           + (f" | disabled: {', '.join(dead)}" if dead else ""),
           "sessions need a real deepgramApiKey in secrets.json")
    if (voice_dir / ".venv" / "deps-ok").exists():
        report(PASS, "voice venv", "bootstrapped (deps-ok sentinel present)")
        model = cfg["voice"].get("wakeModel", "")
        onnx = (voice_dir / ".venv" / "Lib" / "site-packages" / "openwakeword"
                / "resources" / "models" / f"{model}.onnx")
        if onnx.exists():
            report(PASS, "wake model", f"{model}.onnx in the venv")
        else:
            report(WARN, "wake model", f"{model}.onnx not downloaded yet",
                   "auto-fetched on the agent's first run")
    else:
        report(WARN, "voice venv", "not bootstrapped (no .venv\\deps-ok)",
               "run voice\\Start-Voice.bat once (~2 min with network)")
    lib = cglib.BASE / "state" / "library.json"
    try:
        data = json.loads(lib.read_text(encoding="utf-8"))
        age_h = (time.time() - lib.stat().st_mtime) / 3600
        report(PASS, "voice library", f"{len(data.get('installed', []))} installed / "
               f"{len(data.get('owned', []))} owned, refreshed {age_h:.0f}h ago")
    except OSError:
        report(WARN, "voice library", "no index yet",
               "fills itself on the agent's first run (PC awake for installed)")
    except Exception as e:
        report(WARN, "voice library", f"unreadable ({e})",
               "delete state\\library.json; the agent rebuilds it")
    # Tier-3 worker lane - stdlib checks only, same WARN-only posture:
    # background tasks off must never redden the chain doctor.
    import shutil
    wp = cfg["voice"].get("workerProvider", "")
    if wp:
        # provider -> CLI name lives in workers.py and nowhere else; it is
        # stdlib-only, so system python can import it (voice venv deps are
        # still off-limits here).
        sys.path.insert(0, str(voice_dir))
        try:
            from workers import WORKERS
            exe = WORKERS[wp].exe
        except Exception as e:
            exe = None
            report(WARN, "worker CLI", f"can't resolve provider '{wp}' ({e})",
                   "workerProvider is anthropic|openai (see config.example.json)")
        if exe:
            cli = shutil.which(exe)
            if cli:
                report(PASS, "worker CLI", f"{wp} -> {exe} on PATH ({cli})")
            else:
                report(WARN, "worker CLI", f"'{exe}' not on PATH - background "
                       "tasks disabled (everything else runs)",
                       f"npm i -g the {exe} CLI and log in once, "
                       "as the autologon user")
        if not (voice_dir / "worker_home" / "AGENTS.md").exists():
            report(WARN, "worker briefing", "worker_home\\AGENTS.md missing",
                   "git pull should restore it - workers act unbriefed without it")
    jobs_file = cglib.BASE / "state" / "jobs.json"
    if jobs_file.exists():
        try:
            rows = json.loads(jobs_file.read_text(encoding="utf-8"))
            running_jobs = [j for j in rows if j.get("status") == "RUNNING"]
            unread = [j for j in rows if not j.get("read", True)]
            note = (f"{len(rows)} recorded, {len(running_jobs)} running, "
                    f"{len(unread)} unread")
            if running_jobs and "voice_agent" not in _python_cmdlines():
                report(WARN, "worker jobs", note + " - RUNNING with no agent "
                       "(orphan)", "the agent's next start reconciles it to FAILED")
            else:
                report(PASS, "worker jobs", note)
        except Exception as e:
            report(WARN, "worker jobs", f"jobs.json unreadable ({e})",
                   "delete state\\jobs.json; the store recreates it")
    try:
        running = "voice_agent" in _python_cmdlines()
    except Exception as e:
        report(WARN, "voice agent", f"could not detect ({e})", "")
        return
    if running:
        report(PASS, "voice agent", "running (wake word armed)")
    else:
        report(WARN, "voice agent", "not running - wake word deaf (chord unaffected)",
               "run voice\\Start-Voice.bat or the startup shortcut")


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
    check_voice(cfg)
    print(f"\n{_counts[PASS]} pass, {_counts[WARN]} warn, {_counts[FAIL]} fail")
    sys.exit(_counts[FAIL])
