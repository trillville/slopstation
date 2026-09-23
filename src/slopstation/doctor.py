"""K15 chain diagnosis: python -m slopstation.doctor [--smoke]

Read-only except one haptic chirp, skipped when the chord listener is running
(one process owns the Puck). Voice, media and telemetry rows are WARN-only;
only the chord chain can FAIL. Exit code = number of FAILs.

--smoke also asks the running assistant one question, which costs a model
call; the deploy passes it.
"""

import argparse
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from slopstation import config, events, haptics, paths, sessionlock, supervise
from slopstation.agent import operations
from slopstation.agent.media import doctor as media_doctor
from slopstation.agent.steam import library, store

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_counts = {PASS: 0, WARN: 0, FAIL: 0}


def report(level, name, detail, hint=""):
    _counts[level] += 1
    if hint and level != PASS:
        detail = f"{detail}  -> {hint}"
    print(f"[{level}] {name}: {detail}", flush=True)
    # The k15 deploy job sets this: a WARN or FAIL row is then also an
    # annotation on the run, so a green deploy still shows what was found.
    if level != PASS and os.environ.get("SLOPSTATION_ANNOTATE"):
        print(annotation(level, name, detail), flush=True)


def annotation(level, name, message):
    """One row as a GitHub Actions workflow command, escaped the way the runner
    reads it: %, CR and LF everywhere, and : and , in the title as well."""

    def data(text):
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    title = data(name).replace(":", "%3A").replace(",", "%2C")
    kind = "error" if level == FAIL else "warning"
    return f"::{kind} title={title}::{data(message)}"


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
    """One 'is this lane running' row, read from its scheduled task. Returns
    the task row while it runs, else None."""
    try:
        task = supervise.query(lane)
    except Exception as e:
        report(WARN, name, f"could not query the task ({e})", "")
        return None
    if task is None:
        report(
            WARN,
            name,
            f"task {supervise.TASKS[lane]} not registered",
            "run Setup-K15-Tasks.ps1",
        )
        return None
    if task.get("Status") == "Running":
        report(PASS, name, up)
        return task
    report(
        WARN,
        name,
        f"{down} (task {task.get('Status')}, last result {task.get('Last Result')})",
        down_hint,
    )
    return None


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
        dev = haptics.open_streaming_interface()
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
        haptics.chirp(dev)
        report(
            PASS,
            "haptics",
            "chirp sent - you should have felt it "
            "(if not: recheck after a controller firmware update)",
        )
    except Exception as e:
        report(
            FAIL,
            "haptics",
            f"write failed ({e})",
            "protocol drift after firmware update? recheck the report "
            "layouts in haptics.py against SDL's controller_structs.h",
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


def check_wol(cfg):
    if not cfg:
        return
    from slopstation import couch

    ip = cfg.get("gamingPcIp")
    if not ip:
        report(
            FAIL,
            "wake-on-lan",
            "config.json has no gamingPcIp",
            "config broken? see above",
        )
        return
    sources = couch.broadcast_sources()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))
            route = s.getsockname()[0]
    except OSError as e:
        report(
            WARN,
            "wake-on-lan",
            f"no route to {ip} ({e})",
            "PC unplugged, or gamingPcIp wrong?",
        )
        return
    others = [a for a in sources if a != route]
    if route not in sources:
        report(
            FAIL,
            "wake-on-lan",
            f"{route} reaches the PC but is not among {sources or 'no local addresses'}",
            "the wake packet never leaves on the PC's network",
        )
        return
    detail = f"sends from {len(sources)} address(es); {route} reaches the PC"
    if others:
        detail += f", alongside {', '.join(others)}"
    report(PASS, "wake-on-lan", detail)


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
    """Check the USB-over-IP hub used by the gaming PC.

    Windows filters connection setup rather than established flows, so verify
    the firewall rule even when a client is already connected.
    """
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
            "most recent launch failure - see logs\\couch.log",
        )
    except OSError:
        report(PASS, "last_error", "none")


def _steam_mint_probe(days):
    """Can the refresh token actually mint? Returns a report() tuple.

    Shells `steam.session token` (exit 0 = mint works) in this interpreter,
    which is the venv doctor itself runs in. No answer (offline) = PASS.
    """
    try:
        p = subprocess.run(
            [sys.executable, "-m", "slopstation.agent.steam.session", "token"],
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
        "re-run python -m slopstation.agent.steam.session enroll",
    )


def check_voice(cfg, smoke=False):
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
    answered = check_text(cfg)
    if smoke:
        check_assistant(cfg, answered)
    check_remote(cfg)
    check_operations()
    check_voice_agent()


def check_voice_keys():
    secrets = config.secrets()
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
    try:
        summary = library.index_summary()
    except (OSError, ValueError) as e:
        report(
            WARN,
            "voice library",
            f"unreadable ({e})",
            "delete state\\library.json; the agent rebuilds it",
        )
    else:
        if summary is None:
            report(
                WARN,
                "voice library",
                "no index yet",
                "fills itself on the agent's first run (PC awake for installed)",
            )
        else:
            installed, owned, age_h = summary
            report(
                PASS,
                "voice library",
                f"{installed} installed / {owned} owned, refreshed {age_h:.0f}h ago",
            )

    # Deals precompute: the agent refreshes ~6h, so stale means the store sync
    # is failing. WARN past 24h; absent is silent (fills on first sync).
    deals_h = store.deals_age_h()
    if deals_h is not None:
        if deals_h > 24:
            report(
                WARN,
                "voice deals",
                f"stale ({deals_h:.0f}h)",
                "store sync failing, or the agent is down (see 'voice agent')",
            )
        else:
            report(PASS, "voice deals", f"refreshed {deals_h:.0f}h ago")


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
    # present but unusable or near expiry; absent is silent.
    from slopstation.agent.steam import session

    tok = config.secrets().get("steamRefreshToken")
    if not config.real_key(tok):
        return
    exp = session._jwt_exp(tok)  # 0 when unreadable
    days = (exp - time.time()) / 86400 if exp else -1
    if days < 0:
        report(
            WARN,
            "steam session",
            "refresh token unreadable or expired",
            "re-run python -m slopstation.agent.steam.session enroll",
        )
    elif days < 14:
        report(
            WARN,
            "steam session",
            f"token expires in {days:.0f} days",
            "re-scan soon: python -m slopstation.agent.steam.session enroll",
        )
    else:
        # An unexpired web-audience token may still be unable to mint
        # the client token this feature needs.
        report(*_steam_mint_probe(days))


def _tcp_reachable(url, timeout=1):
    parsed = urllib.parse.urlsplit(str(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((parsed.hostname, port), timeout=timeout):
        return True


def check_media(cfg):
    """The media stack's rows, WARN-only: agent/media/doctor.py owns them."""
    media_doctor.check(cfg, config.secrets(), report)


def _text_url(cfg, path):
    """The text interface's URL for `path`, on the host the lane bound. A
    wildcard bind answers on loopback; any other host answers only on itself."""
    text = cfg["textInterface"]
    host = str(text.get("host", "127.0.0.1"))
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{int(text.get('port', 8765))}{path}"


def check_text(cfg):
    """The text interface's /health: what the voice process has up. WARN-only.
    True when it answered."""
    text = cfg.get("textInterface")
    if not isinstance(text, dict) or not text.get("enabled"):
        report(PASS, "text interface", "disabled")
        return False
    token = config.secrets().get("textInterfaceToken")
    if not config.real_key(token):
        report(
            WARN, "text interface", "textInterfaceToken missing or a placeholder", ""
        )
        return False
    port = int(text.get("port", 8765))
    request = urllib.request.Request(
        _text_url(cfg, "/health"), headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as r:
            health = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        report(
            WARN,
            "text interface",
            f"no answer on {port} ({e})",
            "the voice agent hosts it; check the voice lane above",
        )
        return False
    up = [k for k in ("operations", "steam", "media") if health.get(k)]
    # Every thread the voice process started. Missing: never started. False:
    # died.
    threads = health.get("threads") or {}
    dead = sorted(name for name, alive in threads.items() if not alive)
    detail = f"listening on {port}; up: {', '.join(up) or 'nothing'}; threads: {len(threads)}"
    if dead:
        report(WARN, "text interface", f"{detail}; stopped: {', '.join(dead)}", "")
    else:
        report(PASS, "text interface", detail)
    return True


# A question the assistant can only answer by calling a tool, and one whose
# tools are all reads.
SMOKE_QUESTION = (
    "Is the gaming PC awake right now? Only check; do not wake it, start "
    "anything or change anything."
)
SMOKE_TIMEOUT_S = 120


def check_assistant(cfg, text_answered):
    """One real turn through the text interface: the model is reachable, takes
    the tool schemas, calls a tool, and answers. WARN-only."""
    if not text_answered:
        report(WARN, "assistant", "not tried - the text interface did not answer")
        return
    token = config.secrets().get("textInterfaceToken")
    # Its own session, so a conversation in progress is not touched and the
    # trace is easy to tell apart.
    session = f"doctor-{int(time.time())}"
    request = urllib.request.Request(
        _text_url(cfg, "/v1/chat"),
        data=json.dumps({"session": session, "message": SMOKE_QUESTION}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=SMOKE_TIMEOUT_S) as r:
            result = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        report(
            WARN,
            "assistant",
            f"no answer to a question ({e})",
            "see text_request_failed in the voice event log",
        )
        return
    secs = time.monotonic() - started
    tools = result.get("tools") or []
    reply = str(result.get("reply") or "").strip()
    turn = result.get("turn")
    if not reply:
        report(WARN, "assistant", f"turn {turn} came back empty")
    elif not tools:
        report(
            WARN,
            "assistant",
            f"turn {turn} answered without calling a tool: {reply[:80]!r}",
            "the model is not using its tools",
        )
    else:
        report(
            PASS,
            "assistant",
            f"turn {turn} answered in {secs:.0f} s via {', '.join(tools)}",
        )


def check_remote(cfg):
    """The phone lane: MCP wrapper + the tunnel that publishes it. WARN-only."""
    remote = cfg.get("remoteInterface")
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
    try:
        summary = operations.ledger_summary()
    except ValueError as e:
        report(
            WARN,
            "operations",
            f"operations.json unreadable ({e})",
            "restore or remove the file; external work is unaffected but "
            "Slopstation correlation will be lost",
        )
        return
    if summary is None:
        report(PASS, "operations", "no operations recorded")
        return
    note = (
        f"{summary['recorded']} recorded, {summary['active']} active, "
        f"{summary['unknown']} unknown, {summary['pending']} pending announcement"
    )
    # The agent probe is outside the parse: its failure is not a bad ledger.
    try:
        paused = summary["active"] and not supervise.running("voice")
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


# The voice lane's readiness events, newest wins: (row text, hint).
_DEVICE_HINT = "check the audio device in config.json"
READINESS = {
    "audio_ready": ("armed", ""),
    "audio_device_wait": ("waiting for the microphone", _DEVICE_HINT),
    "wake_stream_died": ("mic stream died, rebuilding", _DEVICE_HINT),
    "audio_rebuild_failed": (
        "audio failed to open; retrying every 5 s",
        "see audio_rebuild_failed in the voice event log",
    ),
    "wake_model_missing": (
        "wake model missing",
        "see wake_model_missing in the voice event log",
    ),
    "wake_verifier_missing": (
        "wake verifier missing",
        "see wake_verifier_missing in the voice event log",
    ),
}


def check_voice_agent():
    task = _process_row(
        "voice agent",
        "voice",
        "running",
        "not running - wake word deaf (chord unaffected)",
        "run Start-Slopstation.bat",
    )
    if task is None:
        return
    # The task proves the process, not the wake word: the lane checks in before
    # the mic wait.
    state, hint = READINESS.get(_latest_events(READINESS).get("voice", ""), ("", ""))
    if state == "armed":
        report(PASS, "wake word", "armed")
    elif state:
        report(WARN, "wake word", state, hint)
    elif _started_before_retention(task):
        report(
            PASS,
            "wake word",
            f"no event retained; lane older than {events.TTL_DAYS} days",
        )
    else:
        report(WARN, "wake word", "no readiness event on record", "")


def _started_before_retention(task):
    """True when the task's last start is older than the event retention, so
    an armed lane has no readiness event left to show."""
    try:
        started = time.strptime(task.get("Last Run Time", ""), "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        return False
    return time.mktime(started) < time.time() - events.TTL_DAYS * 86400


def _latest_events(names):
    """Each lane's most recent event among `names`: lane -> event name. Walks
    every retained event file, newest first, because a lane logs a check-in
    once and then only changes. Unparseable lines are skipped."""
    latest: dict = {}
    for f in events.log_files():
        in_file = {}
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not any(f'"{name}"' in line for name in names):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") in names:
                in_file[rec.get("lane")] = rec.get("event")
        for lane, event in in_file.items():
            latest.setdefault(lane, event)
    return latest


def check_sentry(cfg):
    """The DSN, and whether each lane's cron check-in is landing.

    A rejected check-in is the failure worth naming: every Sentry plan
    includes ONE cron monitor, so without a pay-as-you-go budget the second
    lane's monitor never registers - which in Sentry looks exactly like a lane
    that never started."""
    from slopstation import checkin

    parsed = checkin.parse_dsn((cfg or {}).get("sentryDsn"))
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
    seen = _latest_events(("checkin", "checkin_failed"))
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
            "no lane has checked in",
            "expected within a minute of a lane starting; reload with Start-Slopstation.bat",
        )


def check_telemetry(cfg):
    """Event stream written, and anything shipping it? WARN-only."""

    today = events.log_file(time.strftime("%Y%m%d"))  # local date, like events
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
    check_sentry(cfg)
    # SMART needs Administrator for raw device access, so it is a service and
    # not part of any lane; a rebuilt K15 lacks it until someone registers it.
    _service_row(
        "smart watch",
        "smartd",
        "Start-Service smartd - a failing disk says nothing meanwhile",
        "disk attributes unwatched; see smartd.conf.example",
    )


def main(argv=None):
    """Every row, in chain order. Exit code = number of FAILs."""
    ap = argparse.ArgumentParser(prog="slopstation-doctor")
    ap.add_argument(
        "--smoke", action="store_true", help="also ask the assistant one question"
    )
    args = ap.parse_args(argv)
    cfg = check_config()
    check_com(cfg)
    puck_ok = check_puck()
    listener_running = check_listener()
    if puck_ok and not listener_running:
        check_haptics()
    check_ssh()
    check_wol(cfg)
    check_virtualhere()
    check_session_state()
    check_telemetry(cfg)
    check_voice(cfg, smoke=args.smoke)
    print(f"\n{_counts[PASS]} pass, {_counts[WARN]} warn, {_counts[FAIL]} fail")
    return _counts[FAIL]


if __name__ == "__main__":
    sys.exit(main())
