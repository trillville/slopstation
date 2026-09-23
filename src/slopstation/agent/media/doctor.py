"""The doctor's media rows: the config, the containers, Radarr, Sonarr,
Prowlarr, qBittorrent, Proton's forwarded port, Windows' port reservations,
and monitored episodes nothing is chasing.

`slopstation-doctor` runs check() as its media section. Read-only: nothing
here changes a service. A row is PASS or WARN, never FAIL: the stack is
optional, and only the chord chain may fail the doctor and, with it, a deploy.
"""

import json
import subprocess
import time

from slopstation import config, paths
from slopstation.agent import operations
from slopstation.agent.media import proton
from slopstation.agent.media.clients import (
    ArrClient,
    MediaConfigurationError,
    MediaError,
    clean_text,
    kind_spec,
    qbit_from_config,
)

PASS, WARN = "PASS", "WARN"
REQUIRED = (
    "radarrUrl",
    "sonarrUrl",
    "prowlarrUrl",
    "qbittorrentUrl",
    "movieRoot",
    "seriesRoot",
    "moviePresets",
    "seriesPresets",
)
CONTAINERS = ("flaresolverr", "prowlarr", "radarr", "sonarr", "homarr", "glances")
START_MEDIA = "run media\\Start-Media.ps1"
# An episode aired this long ago, still monitored and still missing, is not
# in flight: nothing searches for it, so it is armed for an RSS grab forever.
MONITOR_STALE_DAYS = 7


def check(cfg, secrets, report, now=None):
    """Every media row, through report(level, name, detail, hint)."""
    media_cfg = cfg.get("media")
    if not isinstance(media_cfg, dict) or not media_cfg.get("enabled"):
        report(PASS, "media", "disabled")
        return

    def line(level, name, detail, hint=""):
        # A detail can carry what a service answered: one printable line.
        report(level, name, clean_text(detail, 240), hint)

    _check_config(line, media_cfg)
    _check_containers(line)
    answering = {}
    for name, kind, url_key in (
        ("Radarr", "movie", "radarrUrl"),
        ("Sonarr", "series", "sonarrUrl"),
    ):
        client = _arr_client(line, name, media_cfg.get(url_key, ""), secrets)
        if client is not None and _check_arr(line, kind, client, media_cfg):
            answering[name] = client
    prowlarr = _arr_client(
        line, "Prowlarr", media_cfg.get("prowlarrUrl", ""), secrets, api_version="v1"
    )
    if prowlarr is not None:
        _check_prowlarr(line, prowlarr, media_cfg)
    preferences = _check_qbittorrent(line, media_cfg, secrets)
    if media_cfg.get("protonPortSync"):
        if preferences is not None:
            _check_proton_port_sync(line, preferences, now=now)
        _check_port_reservations(line)
    _check_monitoring(line, answering.get("Sonarr"))


def _check_config(report, media_cfg):
    missing = [key for key in REQUIRED if not media_cfg.get(key)]
    if missing:
        report(
            WARN,
            "media config",
            f"missing keys: {missing}",
            "compare the media block with config.example.json",
        )
    else:
        report(PASS, "media config", "topology, roots, and presets present")


def compose_command(media_dir, *args):
    env_file = media_dir / ".env"
    if not env_file.is_file():
        raise MediaError(f"Compose environment file is missing: {env_file}")
    return [
        "docker",
        "compose",
        "--project-directory",
        str(media_dir),
        "--env-file",
        str(env_file),
        *args,
    ]


def _compose_services(media_dir):
    command = compose_command(media_dir, "ps", "--format", "json")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
    except (FileNotFoundError, OSError) as e:
        raise MediaError("Docker CLI is unavailable") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError("Docker Compose status timed out") from e
    if completed.returncode:
        detail = clean_text(completed.stderr) or "Docker Compose status failed"
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


def _check_containers(report):
    try:
        rows = _compose_services(paths.HOME / "media")
    except MediaError as e:
        report(WARN, "Docker media containers", str(e), START_MEDIA)
        return
    states = {
        str(entry.get("Service", entry.get("service", ""))).casefold(): entry
        for entry in rows
    }
    bad = []
    for name in CONTAINERS:
        entry = states.get(name) or {}
        state = str(entry.get("State", entry.get("state", "")))
        health = str(entry.get("Health", entry.get("health", "")))
        if (
            not entry
            or state.casefold() != "running"
            or health.casefold() not in ("", "healthy")
        ):
            bad.append(name)
    if bad:
        report(
            WARN,
            "Docker media containers",
            "not ready: " + ", ".join(bad),
            START_MEDIA,
        )
    else:
        report(
            PASS,
            "Docker media containers",
            "FlareSolverr, Prowlarr, Radarr, Sonarr, Homarr, and Glances are running",
        )


def _row_field(row, *names):
    wanted = {name.casefold() for name in names}
    for field in row.get("fields") or []:
        if isinstance(field, dict) and str(field.get("name", "")).casefold() in wanted:
            return field.get("value")
    return None


def _number_matches(value, expected):
    try:
        return float(value) == float(expected)
    except (TypeError, ValueError):
        return False


def _enabled_rows(rows):
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("enable", row.get("enabled", True))
    ]


def _arr_client(report, name, url, secrets, api_version="v3"):
    """The client for one Servarr app, or None after a row saying why not."""
    key = f"{name.lower()}ApiKey"
    if not config.real_key(secrets.get(key)):
        report(
            WARN,
            f"{name} API",
            f"{key} is missing",
            f"copy it from {name} > Settings > General into secrets.json",
        )
        return None
    try:
        return ArrClient(name, url, secrets[key], api_version=api_version)
    except MediaConfigurationError as e:
        report(
            WARN,
            f"{name} API",
            str(e),
            "compare the media block with config.example.json",
        )
        return None


def _check_service_reachable(report, client):
    """Status then health - the preamble every Servarr app shares. False means
    the API never answered, so the caller's deeper checks would only restate
    the same failure."""
    label = client.name
    try:
        status = client.get("system/status")
        if not isinstance(status, dict):
            raise MediaError(f"{label} returned invalid status")
        report(
            PASS,
            f"{label} API",
            f"reachable, version {clean_text(status.get('version'), 40)}",
        )
    except MediaError as e:
        report(WARN, f"{label} API", str(e), START_MEDIA)
        return False

    try:
        health = client.get("health")
        if not isinstance(health, list):
            raise MediaError(f"{label} returned invalid health status")
        if health:
            sources = sorted(
                {
                    clean_text(entry.get("source"), 40)
                    for entry in health
                    if isinstance(entry, dict) and entry.get("source")
                }
            )
            detail = f"{len(health)} warning(s)"
            if sources:
                detail += ": " + ", ".join(sources[:5])
            report(WARN, f"{label} health", detail)
        else:
            report(PASS, f"{label} health", "no health warnings")
    except MediaError as e:
        report(WARN, f"{label} health", str(e))
    return True


def _check_arr(report, kind, client, media_cfg):
    """Radarr's or Sonarr's library policy, indexers and download client.
    False when the API did not answer."""
    if not _check_service_reachable(report, client):
        return False
    label = client.name
    spec = kind_spec(kind)
    root_key, presets_key = spec["root_key"], spec["presets_key"]
    try:
        roots = client.get("rootfolder")
        profiles = client.get("qualityprofile")
        if not isinstance(roots, list) or not isinstance(profiles, list):
            raise MediaError(f"{label} returned invalid roots or profiles")
        wanted_root = str(media_cfg.get(root_key, ""))
        wanted = sorted(set((media_cfg.get(presets_key) or {}).values()))
        # Roots compare case-folded and without a trailing separator -
        # Servarr echoes the path back in either form.
        normalized = {
            str(entry.get("path", "")).rstrip("/\\").casefold()
            for entry in roots
            if isinstance(entry, dict)
        }
        root_exists = bool(wanted_root) and (
            wanted_root.rstrip("/\\").casefold() in normalized
        )
        available = {
            str(entry.get("name", "")).casefold()
            for entry in profiles
            if isinstance(entry, dict)
        }
        missing = [name for name in wanted if str(name).casefold() not in available]
        report(
            PASS if root_exists else WARN,
            f"{label} root",
            wanted_root
            if root_exists
            else f"configured root {wanted_root or '(missing)'} does not exist",
        )
        report(
            WARN if missing else PASS,
            f"{label} quality profiles",
            "missing: " + ", ".join(missing)
            if missing
            else f"all {len(wanted)} configured profile(s) exist",
        )
    except MediaError as e:
        report(WARN, f"{label} library policy", str(e))

    try:
        indexers = client.get("indexer")
        if not isinstance(indexers, list):
            raise MediaError(f"{label} returned invalid indexers")
        enabled = _enabled_rows(indexers)
        if enabled:
            report(PASS, f"{label} indexers", f"{len(enabled)} enabled indexer(s)")
        else:
            report(WARN, f"{label} indexers", "no enabled indexers")
    except MediaError as e:
        report(WARN, f"{label} indexers", str(e))

    try:
        clients = client.get("downloadclient")
        if not isinstance(clients, list):
            raise MediaError(f"{label} returned invalid download clients")
        qbittorrent = [
            entry
            for entry in _enabled_rows(clients)
            if str(entry.get("implementation", "")).casefold() == "qbittorrent"
        ]
        expected_category = spec["authority"]
        if not qbittorrent:
            report(
                WARN,
                f"{label} qBittorrent client",
                "no enabled qBittorrent download client",
            )
        else:
            category_field = spec["category_field"]
            categories = {
                clean_text(_row_field(entry, category_field, "category"), 80).casefold()
                for entry in qbittorrent
            }
            if expected_category in categories:
                report(
                    PASS,
                    f"{label} qBittorrent client",
                    f"enabled with {expected_category} category",
                )
            else:
                report(
                    WARN,
                    f"{label} qBittorrent client",
                    f"expected category {expected_category}",
                )
        completed = client.get("config/downloadclient")
        if not isinstance(completed, dict):
            raise MediaError(f"{label} returned invalid download handling")
        handling = bool(completed.get("enableCompletedDownloadHandling"))
        removal = bool(qbittorrent) and all(
            entry.get("removeCompletedDownloads") for entry in qbittorrent
        )
        if not handling:
            report(
                WARN,
                f"{label} completed-download handling",
                "completed-download handling is disabled",
            )
        elif removal:
            report(
                PASS,
                f"{label} completed-download removal",
                "enabled after import and seed-goal completion",
            )
        elif qbittorrent:
            report(
                WARN,
                f"{label} completed-download removal",
                "handling is enabled, but Remove Completed Downloads is disabled on qBittorrent",
            )
    except MediaError as e:
        report(WARN, f"{label} download client", str(e))
    return True


def _check_prowlarr(report, client, media_cfg):
    if not _check_service_reachable(report, client):
        return

    try:
        entries = client.get("indexer")
        if not isinstance(entries, list):
            raise MediaError("Prowlarr returned invalid indexers")
        expected_names = media_cfg.get("managedIndexers") or []
        ratio = media_cfg.get("seedRatio")
        minutes = media_cfg.get("seedTimeMinutes")
        by_name = {
            str(entry.get("name", "")).casefold(): entry
            for entry in entries
            if isinstance(entry, dict)
        }
        for name in expected_names:
            entry = by_name.get(str(name).casefold())
            if entry is None or entry not in _enabled_rows([entry]):
                report(WARN, f"Prowlarr indexer {name}", "missing or disabled")
                continue
            actual_ratio = _row_field(
                entry, "torrentBaseSettings.seedRatio", "seedRatio"
            )
            actual_time = _row_field(entry, "torrentBaseSettings.seedTime", "seedTime")
            if _number_matches(actual_ratio, ratio) and _number_matches(
                actual_time, minutes
            ):
                report(
                    PASS,
                    f"Prowlarr indexer {name}",
                    f"ratio {ratio}, seed time {minutes} minutes",
                )
            else:
                report(
                    WARN,
                    f"Prowlarr indexer {name}",
                    f"expected ratio {ratio} and seed time {minutes} minutes",
                )
        if not expected_names:
            report(WARN, "Prowlarr managed indexers", "media.managedIndexers is empty")
    except MediaError as e:
        report(WARN, "Prowlarr indexers", str(e))

    try:
        entries = client.get("applications")
        if not isinstance(entries, list):
            raise MediaError("Prowlarr returned invalid applications")
        for wanted in ("radarr", "sonarr"):
            matches = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and wanted
                in (
                    str(entry.get("implementation", ""))
                    + " "
                    + str(entry.get("name", ""))
                ).casefold()
            ]
            if not matches:
                report(WARN, f"Prowlarr {wanted} sync", "application is missing")
            elif any(
                "full" in str(entry.get("syncLevel", "")).casefold()
                for entry in matches
            ):
                report(PASS, f"Prowlarr {wanted} sync", "Full Sync")
            else:
                report(WARN, f"Prowlarr {wanted} sync", "Full Sync is not enabled")
    except MediaError as e:
        report(WARN, "Prowlarr applications", str(e))


def _check_qbittorrent(report, media_cfg, secrets):
    """qBittorrent's settings, or None when its API did not answer."""
    try:
        client = qbit_from_config(media_cfg, secrets)
    except MediaConfigurationError as e:
        report(
            WARN,
            "qBittorrent API",
            str(e),
            "compare the media block with config.example.json",
        )
        return None
    try:
        version = client.version()
        preferences = client.preferences()
        categories = client.categories()
        dht_nodes = int(client.transfer_info().get("dht_nodes", 0) or 0)
    except MediaError as e:
        report(
            WARN,
            "qBittorrent API",
            str(e),
            "start qBittorrent; it runs natively, through Proton",
        )
        return None
    report(PASS, "qBittorrent API", f"reachable, version {version}")
    dead = bool(preferences.get("dht", True)) and dht_nodes == 0
    report(
        WARN if dead else PASS,
        "qBittorrent DHT",
        "0 nodes - the peer sockets are dead; restart qBittorrent"
        if dead
        else f"{dht_nodes} nodes",
    )
    expected_interface = str(media_cfg.get("qbittorrentNetworkInterface", "ProtonVPN"))
    interfaces = [
        str(preferences.get(key, ""))
        for key in ("current_network_interface", "current_interface_name")
    ]
    if any(value.casefold() == expected_interface.casefold() for value in interfaces):
        report(PASS, "qBittorrent interface", expected_interface)
    else:
        actual = next((value for value in interfaces if value), "All interfaces")
        report(
            WARN,
            "qBittorrent interface",
            f"expected {expected_interface}; found {actual}",
        )
    address = str(preferences.get("current_interface_address", ""))
    report(
        PASS if not address else WARN,
        "qBittorrent optional IP",
        "All addresses" if not address else f"restricted to {address}",
    )
    report(
        WARN if preferences.get("upnp") else PASS,
        "qBittorrent UPnP/NAT-PMP",
        "enabled" if preferences.get("upnp") else "disabled",
    )
    try:
        port = int(preferences.get("listen_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    report(
        PASS if 1 <= port <= 65535 else WARN,
        "qBittorrent listening port",
        str(port or "invalid"),
    )
    action = preferences.get("max_ratio_act")
    report(
        PASS if action == 0 else WARN,
        "qBittorrent share-limit action",
        "Stop" if action == 0 else "must be Stop, never Remove",
    )
    mode = preferences.get("share_limits_mode")
    if mode is None:
        report(
            PASS,
            "qBittorrent share-limit mode",
            "legacy either-limit behavior (mode field unavailable)",
        )
    else:
        mode_name = str(mode)
        report(
            PASS if mode_name.casefold() == "matchany" else WARN,
            "qBittorrent share-limit mode",
            mode_name or "must be MatchAny (either limit)",
        )
    auth_bypass = preferences.get("bypass_local_auth") or preferences.get(
        "bypass_auth_subnet_whitelist_enabled"
    )
    report(
        WARN if auth_bypass else PASS,
        "qBittorrent Web UI auth",
        "authentication bypass is enabled"
        if auth_bypass
        else "no localhost or subnet bypass",
    )
    # Set by hand in qBittorrent and held only in its own qBittorrent.ini, so
    # a rebuilt client comes back without it and nothing else would say so.
    patterns = {
        part.strip().casefold()
        for part in str(preferences.get("excluded_file_names", ""))
        .replace(",", "\n")
        .split("\n")
        if part.strip()
    }
    if not preferences.get("excluded_file_names_enabled"):
        detail = "disabled - a fake release's program downloads to the drive"
    elif "*.exe" not in patterns:
        detail = "enabled but does not cover *.exe"
    else:
        detail = ""
    report(
        WARN if detail else PASS,
        "qBittorrent excluded file names",
        detail or f"{len(patterns)} patterns, *.exe among them",
    )
    category_names = {str(name).casefold() for name in categories}
    missing = [name for name in ("radarr", "sonarr") if name not in category_names]
    report(
        WARN if missing else PASS,
        "qBittorrent categories",
        "missing: " + ", ".join(missing)
        if missing
        else "radarr and sonarr are present",
    )
    return preferences


def _check_proton_port_sync(report, preferences, now=None):
    try:
        source = proton.read_proton_port_state(now=now)
    except MediaError as e:
        report(WARN, "Proton port synchronization", str(e))
        return
    state = source["state"]
    if state == "active":
        try:
            current = int(preferences.get("listen_port", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        expected = source["port"]
        level = PASS if current == expected else WARN
        detail = (
            f"active port {expected} matches qBittorrent"
            if current == expected
            else f"Proton active port {expected}; qBittorrent uses {current or 'invalid'}"
        )
    elif state == "inactive":
        level, detail = PASS, f"idle; Proton status is {source['status']}"
    elif state == "transitional":
        level = WARN
        detail = f"Proton status is {source['status']}; retry after connection settles"
    elif state == "stale":
        level = WARN
        detail = f"latest Proton state is {source['age_s']:.0f} seconds old"
    elif state == "missing":
        level, detail = WARN, f"client log is missing: {source['path']}"
    else:
        level = WARN
        detail = "client log contains no recognized port-forwarding state"
    report(level, "Proton port synchronization", detail)


def _check_port_reservations(report):
    """Windows reserves blocks of its dynamic port range for Hyper-V and WSL,
    and nothing can bind inside one. While that range reaches Proton's
    forwarded ports, any boot can take qBittorrent's port."""
    ranges = proton.dynamic_port_ranges()
    reaching = [
        f"{protocol.upper()} {first}-{last}"
        for protocol, (first, last) in sorted(ranges.items())
        if last >= proton.PROTON_PORT_FLOOR
    ]
    hint = "move it below 40000: see 'Proton forwarded port' in media\\README.md"
    if len(ranges) < 2:
        report(
            WARN,
            "port reservations",
            "netsh did not report the dynamic port ranges",
            hint,
        )
    elif reaching:
        report(
            WARN,
            "port reservations",
            f"dynamic range {', '.join(reaching)} reaches Proton's forwarded ports",
            hint,
        )
    else:
        report(
            PASS,
            "port reservations",
            "dynamic ranges end below Proton's forwarded ports",
        )


def _check_monitoring(report, sonarr):
    """Monitored-and-missing episodes no active operation owns. Sonarr never
    searches for these, but RSS grabs any NEW upload that matches one - which
    is how an unrequested release arrives."""
    if sonarr is None:
        report(
            WARN,
            "media monitoring",
            "skipped: Sonarr did not answer",
            "see the Sonarr rows above",
        )
        return
    try:
        page = sonarr.get(
            "wanted/missing",
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
        report(WARN, "media monitoring", f"Sonarr did not answer ({e})")
        return
    # ISO-8601 UTC sorts lexicographically, so the cutoff needs no parse.
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - MONITOR_STALE_DAYS * 86400)
    )
    owned = operations.owned_seasons()
    drift: dict = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        aired = record.get("airDateUtc")
        if not isinstance(aired, str) or aired >= cutoff:
            continue  # unaired, or young enough to be in flight
        scope = owned.get(str(record.get("seriesId")), ())
        if scope is None or record.get("seasonNumber") in scope:
            continue
        title = (record.get("series") or {}).get("title") or "?"
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
