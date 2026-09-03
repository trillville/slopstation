"""K15 chain diagnosis: python -m slopstation.doctor

Read-only except one haptic chirp, skipped when the chord listener is running
(one process owns the Puck). Voice and telemetry rows are WARN-only; only the
chord chain can FAIL. Exit code = number of FAILs.
"""

import json
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from slopstation import config, haptics, paths, sessionlock, supervise

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
# An episode aired this long ago, still monitored and still missing, is not
# in flight: nothing searches for it, so it is armed for an RSS grab forever.
MONITOR_STALE_DAYS = 7
_counts = {PASS: 0, WARN: 0, FAIL: 0}


def report(level, name, detail, hint=""):
    _counts[level] += 1
    line = f"[{level}] {name}: {detail}"
    if hint and level != PASS:
        line += f"  -> {hint}"
    print(line, flush=True)


def check_config():
    try:
        cfg = config.load()
    except Exception as e:
        report(
            FAIL,
            "config.json",
            f"unreadable ({e})",
            "recreate from config.example.json",
        )
        return None
    missing = config.missing(cfg)
    n = len(config.REQUIRED)
    if missing:
        report(
            FAIL,
            "config.json",
            f"missing keys: {missing}",
            "compare with config.example.json",
        )
    else:
        report(PASS, "config.json", f"{n}/{n} keys present")
    return cfg


def check_imports():
    for mod in ("serial", "hid"):
        try:
            __import__(mod)
            report(PASS, f"import {mod}", "ok")
        except Exception as e:
            report(
                FAIL,
                f"import {mod}",
                str(e),
                "pip install -e .[dev] -c constraints.txt",
            )


def check_com(cfg):
    if not cfg:
        return
    try:
        import serial

        with serial.Serial(cfg["tvComPort"], 9600, timeout=1):
            pass
        report(PASS, "ex-link port", f"{cfg['tvComPort']} opens")
    except Exception as e:
        report(
            FAIL,
            "ex-link port",
            f"{cfg.get('tvComPort')}: {e}",
            "Device Manager > Ports; SH-U35B unplugged or COM number changed?",
        )


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
    report(
        FAIL,
        "puck",
        "no interfaces for VID 28DE PID 1304",
        "Puck unplugged from the K15, or claimed weirdly - check USB + VirtualHere server",
    )
    return False


def _service_row(name, service, stopped_hint, absent_hint):
    """One row for a Windows service the K15 relies on."""
    try:
        out = subprocess.run(
            ["sc", "query", service], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception as e:
        report(WARN, name, f"could not query ({e})", "")
        return
    if "RUNNING" in out:
        report(PASS, name, f"{service} service running")
    elif "STOPPED" in out:
        report(WARN, name, f"{service} installed but STOPPED", stopped_hint)
    else:
        report(WARN, name, f"{service} not installed", absent_hint)


def _process_row(name, lane, up, down, down_hint):
    """One 'is this lane running' row, read from its scheduled task."""
    try:
        task = supervise.query(lane)
    except Exception as e:
        report(WARN, name, f"could not query the task ({e})", "")
        return False
    if task is None:
        report(
            WARN,
            name,
            f"task {supervise.TASKS[lane]} not registered",
            "run Setup-K15-Tasks.ps1",
        )
        return False
    if task.get("Status") == "Running":
        report(PASS, name, up)
        return True
    report(
        WARN,
        name,
        f"{down} (task {task.get('Status')}, last result {task.get('Last Result')})",
        down_hint,
    )
    return False


def check_listener():
    return _process_row(
        "listener",
        "listener",
        "running (owns the Puck - haptic check skipped)",
        "NOT running - the chord is deaf",
        "run Start-Slopstation.bat (a crashed lane is back within seconds)",
    )


def check_haptics():
    """Only called when the listener is stopped. Needs the controller awake."""
    try:
        dev, _ = haptics.open_streaming_interface(haptics.streams_input_reports)
        why = "no live 0x42 interface"
    except Exception as e:
        dev, why = None, str(e)
    if not dev:
        report(
            WARN,
            "haptics",
            why,
            "controller asleep? tap a button and rerun; or a session is active",
        )
        return
    try:
        haptics.chirp(dev, 0)
        report(
            PASS,
            "haptics",
            "chirp sent - you should have felt it (if not: rerun after firmware calibrate)",
        )
    except Exception as e:
        report(
            FAIL,
            "haptics",
            f"write failed ({e})",
            "protocol drift after firmware update? re-run slopstation.calibrate and slopstation.haptic_test",
        )
    finally:
        dev.close()


def _local_rev():
    """This checkout's short rev - the value Deploy.ps1 stamps on the PC."""
    try:
        r = subprocess.run(
            ["git", "-C", str(paths.HOME), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def check_ssh():
    from slopstation import gamepc

    try:
        config.current()["sshHost"]
    except Exception as e:
        report(
            FAIL,
            "ssh",
            f"config.json has no usable sshHost ({e})",
            "config broken? see above",
        )
        return
    try:
        st = gamepc.status()
        report(
            PASS,
            "ssh status",
            f"-> {st!r} (key, forced command, sshd, firewall all good)",
        )
    except subprocess.TimeoutExpired:
        report(
            WARN,
            "ssh status",
            "timed out",
            "PC asleep? that's normal from idle; wake it to fully test",
        )
    except Exception as e:
        report(
            FAIL,
            "ssh status",
            str(e),
            "PC awake? then check sshd service / firewall rule / administrators_authorized_keys",
        )
        return
    try:
        gamepc.ssh("bogus")  # no wrapper: not a verb, that is the point
        report(
            WARN,
            "ssh dispatch",
            "bogus command did NOT get DENIED",
            "Dispatch.ps1 changed?",
        )
    except subprocess.CalledProcessError as e:
        if "DENIED" in (e.stdout or ""):
            report(PASS, "ssh dispatch", "unknown verbs DENIED")
        else:
            report(
                WARN,
                "ssh dispatch",
                f"unexpected reply {e.stdout!r}",
                "check Dispatch.ps1",
            )
    except Exception as e:
        report(
            WARN,
            "ssh dispatch",
            str(e),
            "transient? status check above is the primary signal",
        )

    # Deploy skew: Deploy.ps1 updates the PC, git pull updates here.
    try:
        pcbuild = gamepc.version()
    except subprocess.CalledProcessError as e:
        if "DENIED" in (e.stdout or ""):
            report(
                WARN,
                "deploy skew",
                "PC's Dispatch predates the version verb",
                "run gaming-pc\\Deploy.ps1 on the PC to ship the current set",
            )
        else:
            report(
                WARN,
                "deploy skew",
                f"version answered {e.stdout!r}",
                "check Dispatch.ps1 on the PC",
            )
        return
    except Exception as e:
        report(WARN, "deploy skew", f"could not query ({e})", "")
        return
    local = _local_rev()
    tok = (pcbuild.split() or [""])[0]
    dirty = tok.endswith("-dirty")
    tok = tok.removesuffix("-dirty")
    if pcbuild == "UNKNOWN":
        report(
            WARN,
            "deploy skew",
            "PC has no build-id stamped",
            "run gaming-pc\\Deploy.ps1 - it stamps what it ships",
        )
    elif not local:
        report(
            WARN,
            "deploy skew",
            f"PC build '{pcbuild}', local rev unreadable (no git?)",
            "",
        )
    elif tok and (tok.startswith(local) or local.startswith(tok)):
        if dirty:
            report(
                WARN,
                "deploy skew",
                f"PC build '{pcbuild}' matches HEAD but shipped from a dirty tree",
                "redeploy from a clean checkout so the rev vouches for the content",
            )
        else:
            report(PASS, "deploy skew", f"PC build '{pcbuild}' matches this checkout")
    else:
        report(
            WARN,
            "deploy skew",
            f"PC build '{pcbuild}' vs local {local}",
            "git pull here and/or Deploy.ps1 there until they agree",
        )


VH_SERVICE = "VirtualHere USB Server"
VH_PORT = "7575"


def _vh_sockets():
    """(listening, connected clients) on the hub port."""
    out = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True,
        text=True,
        timeout=20,
        encoding="utf-8",
        errors="replace",
    ).stdout
    listening = clients = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[1].endswith(":" + VH_PORT):
            continue
        if parts[3] == "LISTENING":
            listening += 1
        elif parts[3] == "ESTABLISHED":
            clients += 1
    return listening, clients


def _vh_lan_rule():
    """Enabled inbound Allow rules admitting the hub on the Private profile,
    or None when netsh answers in a shape this cannot read (localised
    Windows). Get-NetFirewallPortFilter cannot enumerate here and the
    rule-by-rule walk costs 16 s, so this parses netsh's dump (~0.2 s)."""
    out = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"],
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    ).stdout
    if "Rule Name" not in out:
        return None
    found = 0
    for block in out.split(2 * chr(10)):
        f = dict(re.findall(r"^([A-Za-z ]+):\s+(.*?)\s*$", block, re.M))
        if (
            f.get("Enabled") != "Yes"
            or f.get("Action") != "Allow"
            or f.get("Direction") != "In"
        ):
            continue
        profiles = f.get("Profiles", "")
        if "Private" not in profiles and "Any" not in profiles:
            continue
        # Port OR program: a broad any-port rule belongs to its own program,
        # so matching on port alone would pass on someone else's rule.
        ports = [x.strip() for x in f.get("LocalPort", "").split(",")]
        by_port = f.get("Protocol") in ("TCP", "Any") and VH_PORT in ports
        by_program = "vhusbd" in f.get("Program", "").lower()
        found += bool(by_port or by_program)
    return found


def check_virtualhere():
    """The USB-over-IP hub that hands the Puck to the gaming PC; every launch
    claims through it. The firewall row is the load-bearing one: Windows
    filters connection SETUP, not established flows, so a client that
    connected before a rule went wrong keeps working and the hub looks
    healthy until the next restart drops it - which is how a Public-only rule
    on a Private LAN stayed invisible until the first launch after a K15
    reboot (2026-08-30)."""
    try:
        state = subprocess.run(
            ["sc", "query", VH_SERVICE], capture_output=True, text=True, timeout=15
        ).stdout
        listening, clients = _vh_sockets()
        admitting = _vh_lan_rule()
    except Exception as e:
        report(WARN, "virtualhere", f"could not query ({e})", "")
        return
    if "RUNNING" not in state:
        report(
            FAIL,
            "virtualhere",
            "USB server service is not running",
            f"Start-Service '{VH_SERVICE}' - no launch can claim the Puck",
        )
        return
    if not listening:
        report(
            FAIL,
            "virtualhere",
            "service running but nothing listens on " + VH_PORT,
            "restart it; the hub is the Puck only path to the PC",
        )
        return
    if admitting is None:
        report(
            WARN,
            "virtualhere firewall",
            "netsh output not recognised",
            f"check by hand that TCP {VH_PORT} is allowed inbound on Private",
        )
    elif not admitting:
        report(
            FAIL,
            "virtualhere firewall",
            f"no inbound rule admits TCP {VH_PORT} on the Private profile",
            "New-NetFirewallRule -DisplayName 'VirtualHere USB hub (LAN)' "
            "-Direction Inbound -Action Allow -Protocol TCP -LocalPort "
            f"{VH_PORT} -Profile Private -RemoteAddress LocalSubnet",
        )
        return
    # Zero clients is normal: the PC sleeps and reconnects on wake.
    report(
        PASS,
        "virtualhere",
        f"hub listening, LAN rule present, {clients} client(s) connected",
    )


def check_session_state():
    age = sessionlock.age()
    if age is None:
        report(PASS, "session lock", "none (idle)")
    elif sessionlock.active(age):
        report(PASS, "session lock", f"fresh ({age:.0f}s) - a session/launch is active")
    else:
        report(
            WARN,
            "session lock",
            f"stale ({age:.0f}s)",
            "harmless - next launch or reconcile recycles it",
        )
    err = sessionlock.last_error_file()
    try:
        report(
            WARN,
            "last_error",
            err.read_text().strip() or "(empty)",
            "most recent launch failure - see couch.log",
        )
    except OSError:
        report(PASS, "last_error", "none")


def _steam_mint_probe(days):
    """Can the refresh token actually mint? Returns a report() tuple.

    Shells `steam_session token` (exit 0 = mint works) in this interpreter,
    which is the venv doctor itself runs in. No answer (offline) = PASS.
    """
    try:
        p = subprocess.run(
            [sys.executable, "-m", "slopstation.agent.tools.steam_session", "token"],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception as e:
        return (
            PASS,
            "steam session",
            f"enrolled, token good for {days:.0f} days "
            f"(could not verify the mint: {e})",
        )
    if p.returncode == 0:
        return (
            PASS,
            "steam session",
            f"enrolled and minting, refresh token good for {days:.0f} days",
        )
    why = (p.stdout or p.stderr or "").strip().splitlines()
    return (
        WARN,
        "steam session",
        f"enrolled but CANNOT mint - {why[-1][:120] if why else 'unknown'}",
        "install-by-voice falls back to opening the game's page; "
        "re-run python -m slopstation.agent.tools.steam_session enroll",
    )


def check_voice(cfg):
    """Voice overlay health - WARN-only, never FAIL."""
    if not (cfg and isinstance(cfg.get("voice"), dict)):
        report(
            WARN,
            "voice config",
            "no voice section in config.json",
            "copy the voice block from config.example.json to enable voice",
        )
        return
    check_voice_keys()
    check_venv(cfg)
    check_voice_library()
    check_voice_config(cfg)
    check_steam_session()
    check_media(cfg)
    check_media_monitoring(cfg)
    check_remote(cfg)
    check_operations()
    check_voice_agent()


def check_voice_keys():
    try:
        secrets = config.secrets()
    except Exception as e:
        report(
            WARN,
            "voice secrets",
            f"unreadable ({e})",
            "recreate from secrets.template.json",
        )
        secrets = {}
    lanes = {
        "deepgramApiKey": "STT+TTS",
        "anthropicApiKey": "assistant",
        "openaiApiKey": "assistant A/B",
        "steamApiKey": "library owned/meta",
    }
    live = [what for key, what in lanes.items() if config.real_key(secrets.get(key))]
    dead = [
        what for key, what in lanes.items() if not config.real_key(secrets.get(key))
    ]
    report(
        PASS if "STT+TTS" in live else WARN,
        "voice keys",
        f"live: {', '.join(live) or 'none'}"
        + (f" | disabled: {', '.join(dead)}" if dead else ""),
        "sessions need a real deepgramApiKey in secrets.json",
    )


def check_venv(cfg):
    if not supervise.SENTINEL.exists():
        report(
            WARN,
            "venv",
            "not bootstrapped (no deps-ok sentinel)",
            "run Start-Slopstation.bat once (~2 min with network)",
        )
        return
    report(PASS, "venv", "bootstrapped (deps-ok sentinel present)")
    model = cfg["voice"].get("wakeModel", "")
    # Same resolution order as audio.py _resolve_model.
    vendored = pathlib.Path(__file__).parent / "agent" / "models" / f"{model}.onnx"
    pretrained = (
        pathlib.Path(
            sys.prefix, "Lib", "site-packages", "openwakeword", "resources", "models"
        )
        / f"{model}.onnx"
    )
    if vendored.exists():
        report(PASS, "wake model", f"{model}.onnx vendored in {vendored.parent}")
    elif pretrained.exists():
        report(PASS, "wake model", f"{model}.onnx in the venv (pretrained)")
    else:
        report(
            WARN,
            "wake model",
            f"{model}.onnx not present",
            "a pretrained name is fetched on the agent's first run; a custom "
            f"one must be committed to {vendored.parent}",
        )


def check_voice_library():
    lib = paths.state() / "library.json"
    try:
        data = json.loads(lib.read_text(encoding="utf-8"))
        age_h = (time.time() - lib.stat().st_mtime) / 3600
        report(
            PASS,
            "voice library",
            f"{len(data.get('installed', []))} installed / "
            f"{len(data.get('owned', []))} owned, refreshed {age_h:.0f}h ago",
        )
    except OSError:
        report(
            WARN,
            "voice library",
            "no index yet",
            "fills itself on the agent's first run (PC awake for installed)",
        )
    except Exception as e:
        report(
            WARN,
            "voice library",
            f"unreadable ({e})",
            "delete state\\library.json; the agent rebuilds it",
        )

    # Deals precompute: the agent refreshes ~6h, so stale means the store sync
    # is failing. WARN past 24h; absent is silent (fills on first sync).
    deals = paths.state() / "deals.json"
    if deals.exists():
        age_h = (time.time() - deals.stat().st_mtime) / 3600
        if age_h > 24:
            report(
                WARN,
                "voice deals",
                f"stale ({age_h:.0f}h)",
                "store sync failing, or the agent is down (see 'voice agent')",
            )
        else:
            report(PASS, "voice deals", f"refreshed {age_h:.0f}h ago")


def check_voice_config(cfg):
    """Cross-key rules the presence check cannot catch."""
    # A spoken name in both inputs and navTargets makes "show <name>"
    # double-match SwitchInput and Nav; nothing else enforces disjointness.
    v = cfg.get("voice", {})
    clash = set(map(str.lower, v.get("inputs", {}))) & set(
        map(str.lower, v.get("navTargets", {}))
    )
    if clash:
        report(
            WARN,
            "voice config",
            f"inputs/navTargets overlap: {', '.join(sorted(clash))}",
            "rename one side - a shared spoken name double-matches in the grammar",
        )

    # Web search puts untrusted page text into the tool-calling turn.
    # PASS, not WARN - it is a chosen setting.
    if v.get("assistantWebSearch"):
        report(
            PASS,
            "voice web search",
            "on - page text reaches the "
            "tool-calling turn (set assistantWebSearch false to split them)",
        )


def check_steam_session():
    # Account session (install-by-voice). Speaks up only when a token is
    # present but unusable or near expiry; absent is silent. Stdlib only.
    secrets = config.secrets()
    tok = secrets.get("steamRefreshToken")
    if config.real_key(tok):
        try:
            import base64

            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
            days = (exp - time.time()) / 86400 if exp else -1
            if days < 0:
                report(
                    WARN,
                    "steam session",
                    "refresh token unreadable or expired",
                    "re-run python -m slopstation.agent.tools.steam_session enroll",
                )
            elif days < 14:
                report(
                    WARN,
                    "steam session",
                    f"token expires in {days:.0f} days",
                    "re-scan soon: python -m slopstation.agent.tools.steam_session enroll",
                )
            else:
                # Unexpired != working: QR enrolment can yield a web-audience
                # token (aud=[web,renew,derive]) that AccessDenies every mint
                # while reading as good for months (2026-08-14).
                report(*_steam_mint_probe(days))
        except Exception as e:
            report(
                WARN,
                "steam session",
                f"token unreadable ({e})",
                "re-run python -m slopstation.agent.tools.steam_session enroll",
            )


def _tcp_reachable(url, timeout=1):
    parsed = urllib.parse.urlsplit(str(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((parsed.hostname, port), timeout=timeout):
        return True


def check_media(cfg):
    media = cfg.get("media") if isinstance(cfg, dict) else None
    if not isinstance(media, dict) or not media.get("enabled"):
        report(PASS, "media", "disabled")
        return
    required = (
        "radarrUrl",
        "sonarrUrl",
        "prowlarrUrl",
        "qbittorrentUrl",
        "movieRoot",
        "seriesRoot",
        "moviePresets",
        "seriesPresets",
    )
    missing = [key for key in required if not media.get(key)]
    if missing:
        report(
            WARN,
            "media config",
            f"missing keys: {missing}",
            "compare the media block with config.example.json",
        )
    else:
        report(PASS, "media config", "topology, roots, and presets present")
    secrets = config.secrets()
    absent = [
        key
        for key in ("radarrApiKey", "sonarrApiKey")
        if not config.real_key(secrets.get(key))
    ]
    report(
        WARN if absent else PASS,
        "media keys",
        f"missing: {', '.join(absent)}" if absent else "Radarr and Sonarr present",
        "copy each API key from Settings > General into secrets.json",
    )
    reachable, down, unconfigured = [], [], []
    for name, key in (
        ("Prowlarr", "prowlarrUrl"),
        ("Radarr", "radarrUrl"),
        ("Sonarr", "sonarrUrl"),
        ("qBittorrent", "qbittorrentUrl"),
    ):
        if not media.get(key):
            unconfigured.append(name)
            continue
        try:
            _tcp_reachable(media[key])
            reachable.append(name)
        except Exception:
            down.append(name)
    report(
        WARN if down or unconfigured else PASS,
        "media services",
        f"reachable: {', '.join(reachable) or 'none'}"
        + (f" | unreachable: {', '.join(down)}" if down else "")
        + (f" | unconfigured: {', '.join(unconfigured)}" if unconfigured else ""),
        "start media\\Start-Media.ps1 and native qBittorrent",
    )


def _arr_get(url, key, path, params=None, timeout=4):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        str(url).rstrip("/") + path + query, headers={"X-Api-Key": key}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _owned_seasons():
    """series id -> monitored seasons an active operation owns, None meaning
    the whole series. Unreadable ledger owns nothing: the row then over-
    reports, which is the safe direction."""
    owned: dict = {}
    try:
        rows = json.loads(
            (paths.state() / "operations.json").read_text(encoding="utf-8")
        )
        for row in rows if isinstance(rows, list) else ():
            if (
                not isinstance(row, dict)
                or row.get("kind") != "series_acquisition"
                or row.get("state") not in ("QUEUED", "RUNNING", "UNKNOWN")
            ):
                continue
            seasons = (row.get("metadata") or {}).get("seasons")
            key = str(row.get("external_ref"))
            if seasons is None or (key in owned and owned[key] is None):
                owned[key] = None
            else:
                owned.setdefault(key, set()).update(int(n) for n in seasons)
    except Exception:
        return owned
    return owned


def check_media_monitoring(cfg):
    """Monitored-and-missing episodes no active operation owns. Sonarr never
    searches for these, but RSS grabs any NEW upload that matches one - which
    is how an unrequested release arrives. WARN-only."""
    media = cfg.get("media") if isinstance(cfg, dict) else None
    if (
        not isinstance(media, dict)
        or not media.get("enabled")
        or not media.get("sonarrUrl")
    ):
        report(PASS, "media monitoring", "disabled")
        return
    key = config.secrets().get("sonarrApiKey")
    if not config.real_key(key):
        report(
            WARN,
            "media monitoring",
            "no Sonarr API key",
            "copy it from Sonarr Settings > General into secrets.json",
        )
        return
    try:
        page = _arr_get(
            media["sonarrUrl"],
            key,
            "/api/v3/wanted/missing",
            {
                "pageSize": 500,
                "sortKey": "airDateUtc",
                "sortDirection": "descending",
                "monitored": "true",
                "includeSeries": "true",
            },
        )
        records = page.get("records") if isinstance(page, dict) else None
        if not isinstance(records, list):
            raise ValueError("no records in the wanted/missing page")
    except Exception as e:
        report(
            WARN,
            "media monitoring",
            f"Sonarr did not answer ({e})",
            "check the media services row above",
        )
        return
    # ISO-8601 UTC sorts lexicographically, so the cutoff needs no parse.
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - MONITOR_STALE_DAYS * 86400)
    )
    owned = _owned_seasons()
    drift: dict = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        aired = row.get("airDateUtc")
        if not isinstance(aired, str) or aired >= cutoff:
            continue  # unaired, or young enough to be in flight
        scope = owned.get(str(row.get("seriesId")), ())
        if scope is None or row.get("seasonNumber") in scope:
            continue
        title = (row.get("series") or {}).get("title") or "?"
        drift[title] = drift.get(title, 0) + 1
    if not drift:
        report(
            PASS, "media monitoring", "no stale monitored episodes outside active work"
        )
        return
    listed = ", ".join(
        f"{title} ({count})"
        for title, count in sorted(drift.items(), key=lambda kv: -kv[1])[:4]
    )
    report(
        WARN,
        "media monitoring",
        f"{sum(drift.values())} episode(s) armed with nobody chasing them: " + listed,
        "unmonitor the scope you did not ask for; RSS can grab into it",
    )


def check_remote(cfg):
    """The phone lane: MCP wrapper + the tunnel that publishes it. WARN-only."""
    remote = cfg.get("remoteInterface") if isinstance(cfg, dict) else None
    if not isinstance(remote, dict) or not remote.get("enabled"):
        report(PASS, "remote interface", "disabled")
        return
    text = cfg.get("textInterface") or {}
    secrets = config.secrets()
    missing = [
        name
        for name, value in (
            ("remoteInterfaceToken", secrets.get("remoteInterfaceToken")),
            ("textInterfaceToken", secrets.get("textInterfaceToken")),
        )
        if not config.real_key(value)
    ]
    if missing or not text.get("enabled"):
        report(
            WARN,
            "remote config",
            f"missing: {', '.join(missing)}"
            if missing
            else "textInterface is disabled",
            "it forwards to the text interface; both need a real token",
        )
    else:
        report(PASS, "remote config", "token present, forwards to textInterface")
    port = int(remote.get("port", 8766))
    try:
        _tcp_reachable(f"http://127.0.0.1:{port}")
        report(PASS, "remote interface", f"listening on {port}")
    except Exception:
        report(
            WARN,
            "remote interface",
            f"nothing listening on {port}",
            "the voice agent hosts it; check the voice lane above",
        )
    # Without the tunnel the connector cannot reach the K15 at all.
    _service_row(
        "remote tunnel",
        "cloudflared",
        "Start-Service cloudflared - the connector is offline meanwhile",
        "the wrapper is LAN-only until the tunnel is created",
    )


def check_operations():
    operations_file = paths.state() / "operations.json"
    if not operations_file.exists():
        report(PASS, "operations", "no operations recorded")
        return
    try:
        rows = json.loads(operations_file.read_text(encoding="utf-8"))
        active = [o for o in rows if o.get("state") in ("QUEUED", "RUNNING", "UNKNOWN")]
        unknown = [o for o in active if o.get("state") == "UNKNOWN"]
        pending = [o for o in rows if o.get("announcement_pending")]
        note = (
            f"{len(rows)} recorded, {len(active)} active, "
            f"{len(unknown)} unknown, {len(pending)} pending announcement"
        )
    except Exception as e:
        report(
            WARN,
            "operations",
            f"operations.json unreadable ({e})",
            "restore or remove the file; external work is unaffected but "
            "Slopstation correlation will be lost",
        )
        return
    # The agent probe is outside the parse: its failure is not a bad ledger.
    try:
        paused = active and not supervise.running("voice")
    except Exception as e:
        report(
            WARN,
            "operations",
            note + f" - agent probe failed ({e})",
            "the ledger is intact; whether monitoring runs is unknown",
        )
        return
    if paused:
        report(
            WARN,
            "operations",
            note + " - monitoring is paused",
            "start the voice agent; active work will be re-observed",
        )
    else:
        report(PASS, "operations", note)


def check_voice_agent():
    _process_row(
        "voice agent",
        "voice",
        "running (wake word armed)",
        "not running - wake word deaf (chord unaffected)",
        "run Start-Slopstation.bat",
    )


def _tail_records(path, bytes_back=400_000):
    """The last stretch of a daily JSONL as parsed records. A partial first
    line from the seek is dropped, and an unparseable one is skipped: this is
    a diagnosis, not a parser test."""
    start, raw = 0, []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - bytes_back)
            f.seek(start)
            raw = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in raw[1:] if start else raw:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def check_sentry(today):
    """The DSN, and whether each lane's cron check-in is landing.

    A rejected check-in is the failure worth naming: every Sentry plan
    includes ONE cron monitor, so without a pay-as-you-go budget the second
    lane's monitor never registers - which in Sentry looks exactly like a lane
    that never started."""
    from slopstation import checkin

    try:
        dsn = config.load().get("sentryDsn")
    except Exception:
        dsn = None
    parsed = checkin.parse_dsn(dsn)
    if parsed is None:
        report(
            WARN,
            "sentry",
            "sentryDsn not set in config.json",
            "telemetry stays local; see config.example.json",
        )
        return
    report(PASS, "sentry", f"project {parsed[1]} at {parsed[0]}")

    # From the event stream, so this costs no network and cannot create a
    # false check-in for a lane that is actually down.
    seen = {}
    for rec in _tail_records(today):
        if rec.get("event") in ("checkin", "checkin_failed"):
            seen[rec.get("lane")] = rec.get("event")
    failing = sorted(lane for lane, e in seen.items() if e == "checkin_failed")
    if failing:
        report(
            WARN,
            "cron check-in",
            f"rejected for {', '.join(failing)}",
            "a second monitor needs a PAYG budget - Sentry billing settings",
        )
    elif seen:
        report(PASS, "cron check-in", f"accepted for {', '.join(sorted(seen))}")
    else:
        report(
            WARN,
            "cron check-in",
            "no lane has checked in today",
            "expected within a minute of a lane starting; reload with Start-Slopstation.bat",
        )


def check_telemetry():
    """Event stream written, and anything shipping it? WARN-only."""
    from slopstation import events

    today = events._path(time.strftime("%Y%m%d"))  # local date, like events
    try:
        age = time.time() - today.stat().st_mtime
        size_kb = today.stat().st_size / 1024
        report(
            PASS,
            "event stream",
            f"{today.name} {size_kb:.0f} KB, last write {age / 60:.0f} min ago",
        )
    except OSError:
        report(
            WARN,
            "event stream",
            f"{today.name} not written yet",
            "normal on a quiet boot; suspicious if the lanes are up",
        )

    # Retention runs on a process's first emit and at rollover, so old files
    # mean the prune never ran. Scan archive/ too - files move there at
    # ARCHIVE_DAYS and are deleted at TTL_DAYS.
    try:
        stale = [
            f.name
            for f in list(paths.logs().glob("*.jsonl"))
            + list((paths.logs() / events.ARCHIVE_NAME).glob("*.jsonl"))
            if time.time() - f.stat().st_mtime > events.TTL_DAYS * 86400
        ]
        if stale:
            report(
                WARN,
                "event retention",
                f"{len(stale)} file(s) past TTL",
                "harmless; the next rollover prunes them",
            )
    except OSError:
        pass

    # The shipper. Absent is expected until the collector is installed.
    _service_row(
        "log shipper",
        "otelcol-contrib",
        "Start-Service otelcol-contrib - nothing reaches Sentry meanwhile",
        "events are local-only; see otelcol/config.yaml.example",
    )
    check_sentry(today)
    # SMART needs Administrator for raw device access, so it is a service and
    # not part of any lane; a rebuilt K15 lacks it until someone registers it.
    _service_row(
        "smart watch",
        "smartd",
        "Start-Service smartd - a failing disk says nothing meanwhile",
        "disk attributes unwatched; see smartd.conf.example",
    )


def main():
    """Every row, in chain order. Exit code = number of FAILs."""
    cfg = check_config()
    check_imports()
    check_com(cfg)
    puck_ok = check_puck()
    listener_running = check_listener()
    if puck_ok and not listener_running:
        check_haptics()
    check_ssh()
    check_virtualhere()
    check_session_state()
    check_telemetry()
    check_voice(cfg)
    print(f"\n{_counts[PASS]} pass, {_counts[WARN]} warn, {_counts[FAIL]} fail")
    return _counts[FAIL]


if __name__ == "__main__":
    sys.exit(main())
