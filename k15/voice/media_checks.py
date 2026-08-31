"""doctor rows for the media stack: Compose, Radarr/Sonarr, Prowlarr,
qBittorrent, and the Proton port.

The only module that reaches Prowlarr or qBittorrent directly, and only to
report configuration: no release title and no indexer name from an API
response may enter a row. Every indexer name reported comes from
media.managedIndexers, which the operator wrote.
"""

import json
import subprocess
from pathlib import Path

import cglib
from media_clients import (ArrClient, MediaConfigurationError, MediaError,
                           _clean_text, _kind, _qbit_from_config,
                           _root_and_profile_gaps)
from media_proton import read_proton_port_state


class DoctorReport:
    def __init__(self):
        self.checks = []

    def add(self, level, name, detail):
        self.checks.append({"level": level, "name": name,
                            "detail": _clean_text(detail, 240)})

    def result(self):
        return {"ok": not any(row["level"] == "FAIL" for row in self.checks),
                "checks": list(self.checks)}


def _compose_services(media_dir):
    env_file = media_dir / ".env"
    if not env_file.is_file():
        raise MediaError(f"Compose environment file is missing: {env_file}")
    command = [
        "docker", "compose", "--project-directory", str(media_dir),
        "--env-file", str(env_file), "ps", "--format", "json",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=20, check=False)
    except (FileNotFoundError, OSError) as e:
        raise MediaError("Docker CLI is unavailable") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError("Docker Compose status timed out") from e
    if completed.returncode:
        detail = _clean_text(completed.stderr) or "Docker Compose status failed"
        raise MediaError(detail)
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = [rows]
    except ValueError:
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError as e:
            raise MediaError("Docker Compose returned malformed status") from e
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise MediaError("Docker Compose returned invalid status")
    return rows


def _row_field(row, *names):
    wanted = {name.casefold() for name in names}
    for field in row.get("fields") or []:
        if (isinstance(field, dict)
                and str(field.get("name", "")).casefold() in wanted):
            return field.get("value")
    return None


def _number_matches(value, expected):
    try:
        return float(value) == float(expected)
    except (TypeError, ValueError):
        return False


def _enabled_rows(rows):
    return [row for row in rows if isinstance(row, dict)
            and row.get("enable", row.get("enabled", True))]


def _check_service_reachable(report, client):
    """Status then health - the preamble every Servarr app shares. False means
    the API never answered, so the caller's deeper checks would only restate
    the same failure."""
    label = client.name
    try:
        status = client.get("system/status")
        if not isinstance(status, dict):
            raise MediaError(f"{label} returned invalid status")
        report.add("PASS", f"{label} API",
                   f"reachable, version {_clean_text(status.get('version'), 40)}")
    except MediaError as e:
        report.add("FAIL", f"{label} API", str(e))
        return False

    try:
        health = client.get("health")
        if not isinstance(health, list):
            raise MediaError(f"{label} returned invalid health status")
        if health:
            sources = sorted({_clean_text(row.get("source"), 40) for row in health
                              if isinstance(row, dict) and row.get("source")})
            detail = f"{len(health)} warning(s)"
            if sources:
                detail += ": " + ", ".join(sources[:5])
            report.add("WARN", f"{label} health", detail)
        else:
            report.add("PASS", f"{label} health", "no health warnings")
    except MediaError as e:
        report.add("FAIL", f"{label} health", str(e))
    return True


def _check_arr(report, kind, client, media_cfg):
    if not _check_service_reachable(report, client):
        return
    label = client.name
    spec = _kind(kind)
    root_key, presets_key = spec["root_key"], spec["presets_key"]
    try:
        roots = client.get("rootfolder")
        profiles = client.get("qualityprofile")
        if not isinstance(roots, list) or not isinstance(profiles, list):
            raise MediaError(f"{label} returned invalid roots or profiles")
        wanted_root = str(media_cfg.get(root_key, ""))
        wanted = sorted(set((media_cfg.get(presets_key) or {}).values()))
        root_exists, missing = _root_and_profile_gaps(
            [row.get("path", "") for row in roots if isinstance(row, dict)],
            [row.get("name", "") for row in profiles if isinstance(row, dict)],
            wanted_root, wanted)
        if root_exists:
            report.add("PASS", f"{label} root", wanted_root)
        else:
            report.add("FAIL", f"{label} root",
                       f"configured root {wanted_root or '(missing)'} does not exist")
        if missing:
            report.add("FAIL", f"{label} quality profiles",
                       "missing: " + ", ".join(missing))
        else:
            report.add("PASS", f"{label} quality profiles",
                       f"all {len(wanted)} configured profile(s) exist")
    except MediaError as e:
        report.add("FAIL", f"{label} library policy", str(e))

    try:
        indexers = client.get("indexer")
        if not isinstance(indexers, list):
            raise MediaError(f"{label} returned invalid indexers")
        enabled = _enabled_rows(indexers)
        if enabled:
            report.add("PASS", f"{label} indexers",
                       f"{len(enabled)} enabled indexer(s)")
        else:
            report.add("FAIL", f"{label} indexers", "no enabled indexers")
    except MediaError as e:
        report.add("FAIL", f"{label} indexers", str(e))

    try:
        clients = client.get("downloadclient")
        if not isinstance(clients, list):
            raise MediaError(f"{label} returned invalid download clients")
        qbittorrent = [row for row in _enabled_rows(clients)
                       if str(row.get("implementation", "")).casefold()
                       == "qbittorrent"]
        expected_category = spec["authority"]
        if not qbittorrent:
            report.add("FAIL", f"{label} qBittorrent client",
                       "no enabled qBittorrent download client")
        else:
            category_field = spec["category_field"]
            categories = {_clean_text(
                _row_field(row, category_field, "category"), 80).casefold()
                          for row in qbittorrent}
            if expected_category in categories:
                report.add("PASS", f"{label} qBittorrent client",
                           f"enabled with {expected_category} category")
            else:
                report.add("FAIL", f"{label} qBittorrent client",
                           f"expected category {expected_category}")
        completed = client.get("config/downloadclient")
        if not isinstance(completed, dict):
            raise MediaError(f"{label} returned invalid download handling")
        handling = bool(completed.get("enableCompletedDownloadHandling"))
        removal = bool(qbittorrent) and all(
            row.get("removeCompletedDownloads") for row in qbittorrent)
        if not handling:
            report.add("FAIL", f"{label} completed-download handling",
                       "completed-download handling is disabled")
        elif handling and removal:
            report.add("PASS", f"{label} completed-download removal",
                       "enabled after import and seed-goal completion")
        elif handling and qbittorrent:
            report.add("WARN", f"{label} completed-download removal",
                       "handling is enabled, but Remove Completed Downloads is disabled on qBittorrent")
    except MediaError as e:
        report.add("FAIL", f"{label} download client", str(e))


def _check_prowlarr(report, client, media_cfg):
    if not _check_service_reachable(report, client):
        return

    try:
        rows = client.get("indexer")
        if not isinstance(rows, list):
            raise MediaError("Prowlarr returned invalid indexers")
        expected_names = media_cfg.get("managedIndexers") or []
        ratio = media_cfg.get("seedRatio")
        minutes = media_cfg.get("seedTimeMinutes")
        by_name = {str(row.get("name", "")).casefold(): row
                   for row in rows if isinstance(row, dict)}
        for name in expected_names:
            row = by_name.get(str(name).casefold())
            if row is None or row not in _enabled_rows([row]):
                report.add("FAIL", f"Prowlarr indexer {name}", "missing or disabled")
                continue
            actual_ratio = _row_field(
                row, "torrentBaseSettings.seedRatio", "seedRatio")
            actual_time = _row_field(
                row, "torrentBaseSettings.seedTime", "seedTime")
            if (_number_matches(actual_ratio, ratio)
                    and _number_matches(actual_time, minutes)):
                report.add("PASS", f"Prowlarr indexer {name}",
                           f"ratio {ratio}, seed time {minutes} minutes")
            else:
                report.add("FAIL", f"Prowlarr indexer {name}",
                           f"expected ratio {ratio} and seed time {minutes} minutes")
        if not expected_names:
            report.add("WARN", "Prowlarr managed indexers",
                       "media.managedIndexers is empty")
    except MediaError as e:
        report.add("FAIL", "Prowlarr indexers", str(e))

    try:
        rows = client.get("applications")
        if not isinstance(rows, list):
            raise MediaError("Prowlarr returned invalid applications")
        for wanted in ("radarr", "sonarr"):
            matches = [row for row in rows if isinstance(row, dict)
                       and wanted in (str(row.get("implementation", "")) + " "
                                      + str(row.get("name", ""))).casefold()]
            if not matches:
                report.add("FAIL", f"Prowlarr {wanted} sync", "application is missing")
            elif any("full" in str(row.get("syncLevel", "")).casefold()
                     for row in matches):
                report.add("PASS", f"Prowlarr {wanted} sync", "Full Sync")
            else:
                report.add("FAIL", f"Prowlarr {wanted} sync", "Full Sync is not enabled")
    except MediaError as e:
        report.add("FAIL", "Prowlarr applications", str(e))


def _check_qbittorrent(report, client, media_cfg):
    try:
        version = client.version()
        preferences = client.preferences()
        categories = client.categories()
    except MediaError as e:
        report.add("FAIL", "qBittorrent API", str(e))
        return None
    report.add("PASS", "qBittorrent API", f"reachable, version {version}")
    expected_interface = str(media_cfg.get(
        "qbittorrentNetworkInterface", "ProtonVPN"))
    interfaces = [str(preferences.get(key, "")) for key in
                  ("current_network_interface", "current_interface_name")]
    if any(value.casefold() == expected_interface.casefold() for value in interfaces):
        report.add("PASS", "qBittorrent interface", expected_interface)
    else:
        actual = next((value for value in interfaces if value), "All interfaces")
        report.add("FAIL", "qBittorrent interface",
                   f"expected {expected_interface}; found {actual}")
    address = str(preferences.get("current_interface_address", ""))
    report.add("PASS" if not address else "WARN", "qBittorrent optional IP",
               "All addresses" if not address else f"restricted to {address}")
    report.add("FAIL" if preferences.get("upnp") else "PASS",
               "qBittorrent UPnP/NAT-PMP",
               "enabled" if preferences.get("upnp") else "disabled")
    try:
        port = int(preferences.get("listen_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    report.add("PASS" if 1 <= port <= 65535 else "FAIL",
               "qBittorrent listening port", str(port or "invalid"))
    action = preferences.get("max_ratio_act")
    report.add("PASS" if action == 0 else "FAIL", "qBittorrent share-limit action",
               "Stop" if action == 0 else "must be Stop, never Remove")
    mode = preferences.get("share_limits_mode")
    if mode is None:
        report.add("PASS", "qBittorrent share-limit mode",
                   "legacy either-limit behavior (mode field unavailable)")
    else:
        mode_name = str(mode)
        report.add("PASS" if mode_name.casefold() == "matchany" else "FAIL",
                   "qBittorrent share-limit mode",
                   mode_name or "must be MatchAny (either limit)")
    auth_bypass = (preferences.get("bypass_local_auth")
                   or preferences.get("bypass_auth_subnet_whitelist_enabled"))
    report.add("FAIL" if auth_bypass else "PASS", "qBittorrent Web UI auth",
               "authentication bypass is enabled" if auth_bypass
               else "no localhost or subnet bypass")
    category_names = {str(name).casefold() for name in categories}
    missing = [name for name in ("radarr", "sonarr")
               if name not in category_names]
    report.add("FAIL" if missing else "PASS", "qBittorrent categories",
               "missing: " + ", ".join(missing) if missing
               else "radarr and sonarr are present")
    return preferences


def _check_proton_port_sync(report, preferences, path=None, now=None):
    try:
        source = read_proton_port_state(path=path, now=now)
    except MediaError as e:
        report.add("FAIL", "Proton port synchronization", str(e))
        return
    state = source["state"]
    if state == "active":
        try:
            current = int(preferences.get("listen_port", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        expected = source["port"]
        report.add("PASS" if current == expected else "FAIL",
                   "Proton port synchronization",
                   (f"active port {expected} matches qBittorrent"
                    if current == expected else
                    f"Proton active port {expected}; qBittorrent uses {current or 'invalid'}"))
    elif state == "inactive":
        report.add("PASS", "Proton port synchronization",
                   f"idle; Proton status is {source['status']}")
    elif state == "transitional":
        report.add("WARN", "Proton port synchronization",
                   f"Proton status is {source['status']}; retry after connection settles")
    elif state == "stale":
        report.add("FAIL", "Proton port synchronization",
                   f"latest Proton state is {source['age_s']:.0f} seconds old")
    elif state == "missing":
        report.add("FAIL", "Proton port synchronization",
                   f"client log is missing: {source['path']}")
    else:
        report.add("FAIL", "Proton port synchronization",
                   "client log contains no recognized port-forwarding state")


def media_doctor(cfg, secrets, log, arr_transport=None, qbit_transport=None,
                 compose_runner=None, proton_log_path=None, now=None):
    """Read live configuration without changing any service."""
    report = DoctorReport()
    media_cfg = cfg.get("media") if isinstance(cfg, dict) else None
    if not isinstance(media_cfg, dict):
        report.add("FAIL", "Slopstation media config", "media section is missing")
        return report.result()
    report.add("PASS" if media_cfg.get("enabled") else "FAIL",
               "Slopstation media config",
               "enabled" if media_cfg.get("enabled") else "media.enabled is false")

    media_dir = Path(__file__).resolve().parent.parent / "media"
    try:
        rows = (compose_runner or _compose_services)(media_dir)
        states = {str(row.get("Service", row.get("service", ""))).casefold(): row
                  for row in rows}
        bad = []
        for name in ("flaresolverr", "prowlarr", "radarr", "sonarr"):
            row = states.get(name)
            state = str((row or {}).get("State", (row or {}).get("state", "")))
            health = str((row or {}).get("Health", (row or {}).get("health", "")))
            if (row is None or state.casefold() != "running"
                    or health.casefold() not in ("", "healthy")):
                bad.append(name)
        report.add("FAIL" if bad else "PASS", "Docker media containers",
                   "not ready: " + ", ".join(bad) if bad
                   else "FlareSolverr, Prowlarr, Radarr, and Sonarr are running")
    except MediaError as e:
        report.add("FAIL", "Docker media containers", str(e))

    clients = {}
    for name, key, url_key in (("Radarr", "radarrApiKey", "radarrUrl"),
                               ("Sonarr", "sonarrApiKey", "sonarrUrl")):
        if not cglib.real_key(secrets.get(key)):
            report.add("FAIL", f"{name} API", f"{key} is missing")
            continue
        try:
            clients[name] = ArrClient(name, media_cfg.get(url_key, ""),
                                      secrets[key], transport=arr_transport)
        except MediaConfigurationError as e:
            report.add("FAIL", f"{name} API", str(e))
    if "Radarr" in clients:
        _check_arr(report, "movie", clients["Radarr"], media_cfg)
    if "Sonarr" in clients:
        _check_arr(report, "series", clients["Sonarr"], media_cfg)

    if cglib.real_key(secrets.get("prowlarrApiKey")):
        try:
            prowlarr = ArrClient(
                "Prowlarr", media_cfg.get("prowlarrUrl", ""),
                secrets["prowlarrApiKey"], api_version="v1",
                transport=arr_transport)
            _check_prowlarr(report, prowlarr, media_cfg)
        except MediaConfigurationError as e:
            report.add("FAIL", "Prowlarr API", str(e))
    else:
        report.add("FAIL", "Prowlarr API", "prowlarrApiKey is missing")

    try:
        qbit_preferences = _check_qbittorrent(
            report, _qbit_from_config(media_cfg, secrets, qbit_transport),
            media_cfg)
        if media_cfg.get("protonPortSync") and qbit_preferences is not None:
            _check_proton_port_sync(
                report, qbit_preferences, path=proton_log_path, now=now)
    except MediaConfigurationError as e:
        report.add("FAIL", "qBittorrent API", str(e))
    return report.result()
