"""K15 chain diagnosis: python doctor.py

Read-only except one haptic chirp, skipped when the chord listener is running
(one process owns the Puck). Voice and telemetry rows are WARN-only; only the
chord chain can FAIL. Exit code = number of FAILs.
"""
import json, subprocess, sys, time

import cglib
import haptics

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_counts = {PASS: 0, WARN: 0, FAIL: 0}


def report(level, name, detail, hint=""):
    _counts[level] += 1
    line = f"[{level}] {name}: {detail}"
    if hint and level != PASS:
        line += f"  -> {hint}"
    print(line, flush=True)


def check_config():
    try:
        cfg = cglib.load_config()
    except Exception as e:
        report(FAIL, "config.json", f"unreadable ({e})", "recreate from k15/config.example.json")
        return None
    missing = cglib.missing_config(cfg)
    n = len(cglib.REQUIRED_CONFIG)
    if missing:
        report(FAIL, "config.json", f"missing keys: {missing}", "compare with k15/config.example.json")
    else:
        report(PASS, "config.json", f"{n}/{n} keys present")
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
        n = len(hid.enumerate(haptics.VID, haptics.PID))
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


def _local_rev():
    """This checkout's short rev - the value Deploy.ps1 stamps on the PC."""
    try:
        r = subprocess.run(["git", "-C", str(cglib.BASE), "rev-parse",
                            "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def check_ssh():
    import gamepc
    ssh = gamepc.ssh
    try:
        cglib.config()["sshHost"]
    except Exception as e:
        report(FAIL, "ssh", f"config.json has no usable sshHost ({e})", "config broken? see above")
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

    # Deploy skew: Deploy.ps1 updates the PC, git pull updates here.
    try:
        pcbuild = ssh("version")
    except subprocess.CalledProcessError as e:
        if "DENIED" in (e.stdout or ""):
            report(WARN, "deploy skew", "PC's Dispatch predates the version verb",
                   "run gaming-pc\\Deploy.ps1 on the PC to ship the current set")
        else:
            report(WARN, "deploy skew", f"version answered {e.stdout!r}",
                   "check Dispatch.ps1 on the PC")
        return
    except Exception as e:
        report(WARN, "deploy skew", f"could not query ({e})", "")
        return
    local = _local_rev()
    tok = (pcbuild.split() or [""])[0]
    dirty = tok.endswith("-dirty")
    tok = tok.removesuffix("-dirty")
    if pcbuild == "UNKNOWN":
        report(WARN, "deploy skew", "PC has no build-id stamped",
               "run gaming-pc\\Deploy.ps1 - it stamps what it ships")
    elif not local:
        report(WARN, "deploy skew",
               f"PC build '{pcbuild}', local rev unreadable (no git?)", "")
    elif tok and (tok.startswith(local) or local.startswith(tok)):
        if dirty:
            report(WARN, "deploy skew",
                   f"PC build '{pcbuild}' matches HEAD but shipped from a dirty tree",
                   "redeploy from a clean checkout so the rev vouches for the content")
        else:
            report(PASS, "deploy skew", f"PC build '{pcbuild}' matches this checkout")
    else:
        report(WARN, "deploy skew", f"PC build '{pcbuild}' vs local {local}",
               "git pull here and/or Deploy.ps1 there until they agree")


def check_session_state():
    age = cglib.lock_age()
    if age is None:
        report(PASS, "session lock", "none (idle)")
    elif cglib.session_active(age):
        report(PASS, "session lock", f"fresh ({age:.0f}s) - a session/launch is active")
    else:
        report(WARN, "session lock", f"stale ({age:.0f}s)",
               "harmless - next launch or reconcile recycles it")
    err = cglib.LAST_ERROR
    try:
        report(WARN, "last_error", err.read_text().strip() or "(empty)",
               "most recent launch failure - see couch.log")
    except OSError:
        report(PASS, "last_error", "none")


def _steam_mint_probe(days):
    """Can the refresh token actually mint? Returns a report() tuple.

    Shells `steam_session.py token` (exit 0 = mint works) in the voice venv,
    since doctor runs on system python. No answer (no venv, offline) = PASS.
    """
    vpy = cglib.BASE / "voice" / ".venv" / "Scripts" / "python.exe"
    script = cglib.BASE / "voice" / "steam_session.py"
    if not vpy.exists():
        return (PASS, "steam session",
                f"enrolled, token good for {days:.0f} days (venv absent, mint unchecked)")
    try:
        p = subprocess.run([str(vpy), str(script), "token"],
                           capture_output=True, text=True, timeout=45)
    except Exception as e:
        return (PASS, "steam session",
                f"enrolled, token good for {days:.0f} days "
                f"(could not verify the mint: {e})")
    if p.returncode == 0:
        return (PASS, "steam session",
                f"enrolled and minting, refresh token good for {days:.0f} days")
    why = (p.stdout or p.stderr or "").strip().splitlines()
    return (WARN, "steam session",
            f"enrolled but CANNOT mint - {why[-1][:120] if why else 'unknown'}",
            "install-by-voice falls back to opening the game's page; "
            "re-run k15\\voice\\steam_session.py enroll to restore it")


def check_voice(cfg):
    """Voice overlay health - WARN-only, never FAIL. Filesystem and process
    checks only; doctor runs on system python, without the voice venv deps."""
    if not (cfg and isinstance(cfg.get("voice"), dict)):
        report(WARN, "voice config", "no voice section in config.json",
               "copy the voice block from config.example.json to enable voice")
        return
    check_voice_keys()
    check_voice_venv(cfg)
    check_voice_library()
    check_voice_config(cfg)
    check_steam_session()
    check_workers(cfg)
    check_voice_agent()


def check_voice_keys():
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


def check_voice_venv(cfg):
    voice_dir = cglib.BASE / "voice"
    if (voice_dir / ".venv" / "deps-ok").exists():
        report(PASS, "voice venv", "bootstrapped (deps-ok sentinel present)")
        model = cfg["voice"].get("wakeModel", "")
        # Same resolution order as audio.py _resolve_model.
        vendored = voice_dir / "models" / f"{model}.onnx"
        onnx = (voice_dir / ".venv" / "Lib" / "site-packages" / "openwakeword"
                / "resources" / "models" / f"{model}.onnx")
        if vendored.exists():
            report(PASS, "wake model", f"{model}.onnx vendored in voice\\models")
        elif onnx.exists():
            report(PASS, "wake model", f"{model}.onnx in the venv (pretrained)")
        else:
            report(WARN, "wake model", f"{model}.onnx not present",
                   "a pretrained name is auto-fetched on the agent's first "
                   "run; a custom one has no upstream and must be committed "
                   "to voice\\models")
    else:
        report(WARN, "voice venv", "not bootstrapped (no .venv\\deps-ok)",
               "run voice\\Start-Voice.bat once (~2 min with network)")


def check_voice_library():
    lib = cglib.STATE / "library.json"
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

    # Deals precompute: the agent refreshes ~6h, so stale means the store sync
    # is failing. WARN past 24h; absent is silent (fills on first sync).
    deals = cglib.STATE / "deals.json"
    if deals.exists():
        age_h = (time.time() - deals.stat().st_mtime) / 3600
        if age_h > 24:
            report(WARN, "voice deals", f"stale ({age_h:.0f}h)",
                   "store sync failing, or the agent is down (see 'voice agent')")
        else:
            report(PASS, "voice deals", f"refreshed {age_h:.0f}h ago")


def check_voice_config(cfg):
    """Cross-key rules the presence check cannot catch."""
    # A spoken name in both inputs and navTargets makes "show <name>"
    # double-match SwitchInput and Nav; nothing else enforces disjointness.
    v = cfg.get("voice", {})
    clash = set(map(str.lower, v.get("inputs", {}))) & set(map(str.lower, v.get("navTargets", {})))
    if clash:
        report(WARN, "voice config", f"inputs/navTargets overlap: {', '.join(sorted(clash))}",
               "rename one side - a shared spoken name double-matches in the grammar")

    # Web search puts untrusted page text into the tool-calling turn.
    # PASS, not WARN - it is a chosen setting.
    if v.get("assistantWebSearch"):
        report(PASS, "voice web search", "on - page text reaches the "
               "tool-calling turn (set assistantWebSearch false to split them)")


def check_steam_session():
    # Account session (install-by-voice). Speaks up only when a token is
    # present but unusable or near expiry; absent is silent. Stdlib only.
    secrets = cglib.load_secrets()
    tok = secrets.get("steamRefreshToken")
    if cglib.real_key(tok):
        try:
            import base64
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
            days = (exp - time.time()) / 86400 if exp else -1
            if days < 0:
                report(WARN, "steam session", "refresh token unreadable or expired",
                       "re-run k15\\voice\\steam_session.py enroll")
            elif days < 14:
                report(WARN, "steam session", f"token expires in {days:.0f} days",
                       "re-scan soon: steam_session.py enroll")
            else:
                # Unexpired != working: QR enrolment can yield a web-audience
                # token (aud=[web,renew,derive]) that AccessDenies every mint
                # while reading as good for months (2026-08-14).
                report(*_steam_mint_probe(days))
        except Exception as e:
            report(WARN, "steam session", f"token unreadable ({e})",
                   "re-run steam_session.py enroll")


def _worker_exe(voice_dir, wp):
    """The provider's CLI name, asked of workers.py in the voice venv - doctor
    runs on system python and imports nothing from voice/. None when the venv
    is absent or the provider is unknown."""
    vpy = voice_dir / ".venv" / "Scripts" / "python.exe"
    if not vpy.exists():
        return None
    try:
        p = subprocess.run(
            [str(vpy), "-c", f"import workers; print(workers.WORKERS[{wp!r}].exe)"],
            cwd=str(voice_dir), capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    exe = p.stdout.strip()
    return exe if p.returncode == 0 and exe else None


def check_workers(cfg):
    voice_dir = cglib.BASE / "voice"
    # Background-task lane - WARN-only.
    import shutil
    wp = cfg["voice"].get("workerProvider", "")
    if wp:
        exe = _worker_exe(voice_dir, wp)
        if exe is None:
            report(WARN, "worker CLI", f"can't resolve provider '{wp}'",
                   "workerProvider is anthropic|openai (see config.example.json); "
                   "needs the voice venv (voice\\Start-Voice.bat once)")
        else:
            cli = shutil.which(exe)
            if cli:
                report(PASS, "worker CLI", f"{wp} -> {exe} on PATH ({cli})")
            else:
                report(WARN, "worker CLI", f"'{exe}' not on PATH - background "
                       "tasks disabled (everything else runs)",
                       f"npm i -g the {exe} CLI and log in once, "
                       "as the autologon user")
        if wp == "openai":
            # Codex keeps a shell: the boundary here is AGENTS.md policy,
            # not the harness (see workers.py).
            report(WARN, "worker isolation",
                   "codex keeps a shell (sandbox confines writes, not reads)",
                   "structural research-only isolation is the anthropic lane")
        if not (voice_dir / "worker_home" / "AGENTS.md").exists():
            report(WARN, "worker briefing", "worker_home\\AGENTS.md missing",
                   "git pull should restore it - workers act unbriefed without it")
    jobs_file = cglib.STATE / "jobs.json"
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


def check_voice_agent():
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


def check_telemetry():
    """Event stream written, and anything shipping it? WARN-only."""
    import events
    today = events._path(time.strftime("%Y%m%d"))   # local date, like events
    try:
        age = time.time() - today.stat().st_mtime
        size_kb = today.stat().st_size / 1024
        report(PASS, "event stream",
               f"{today.name} {size_kb:.0f} KB, last write {age / 60:.0f} min ago")
    except OSError:
        report(WARN, "event stream", f"{today.name} not written yet",
               "normal on a quiet boot; suspicious if the lanes are up")

    # Retention runs on a process's first emit and at rollover, so old files
    # mean the prune never ran. Scan archive/ too - files move there at
    # ARCHIVE_DAYS and are deleted at TTL_DAYS.
    try:
        stale = [f.name for f in
                 list(events.LOG_DIR.glob("*.jsonl")) +
                 list((events.LOG_DIR / events.ARCHIVE_NAME).glob("*.jsonl"))
                 if time.time() - f.stat().st_mtime > events.TTL_DAYS * 86400]
        if stale:
            report(WARN, "event retention", f"{len(stale)} file(s) past TTL",
                   "harmless; the next rollover prunes them")
    except OSError:
        pass

    # The shipper. Absent is expected until Alloy is installed.
    try:
        out = subprocess.run(["sc", "query", "Alloy"], capture_output=True,
                             text=True, timeout=10).stdout
        if "RUNNING" in out:
            report(PASS, "log shipper", "Alloy service running")
        elif "STOPPED" in out:
            report(WARN, "log shipper", "Alloy installed but STOPPED",
                   "Start-Service Alloy - nothing reaches Grafana meanwhile")
        else:
            report(WARN, "log shipper", "Alloy not installed",
                   "events are local-only; see alloy/config.alloy.example")
    except Exception as e:
        report(WARN, "log shipper", f"could not query ({e})", "")


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
    check_telemetry()
    check_voice(cfg)
    print(f"\n{_counts[PASS]} pass, {_counts[WARN]} warn, {_counts[FAIL]} fail")
    sys.exit(_counts[FAIL])
