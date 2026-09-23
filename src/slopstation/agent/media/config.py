"""Build the media service and its watches from config.json."""

from pathlib import Path

from slopstation import config, paths
from slopstation.agent.media.clients import (
    ArrClient,
    MediaConfigurationError,
    qbit_from_config,
)
from slopstation.agent.media.disk import (
    DISK_POLL_S,
    FREE_WARN_BYTES,
    DiskHealthMonitor,
)
from slopstation.agent.media.health import HEALTH_POLL_S, MediaHealthMonitor
from slopstation.agent.media.proton import (
    ProtonPortMonitor,
)
from slopstation.agent.media.service import MediaService
from slopstation.agent.media.units import GB
from slopstation.agent.media.updates import MediaUpdateMonitor


def _media_cfg(cfg, flag=None, default=True):
    """The media section while the lane, and `flag` if given, are on."""
    media_cfg = cfg.get("media")
    if not isinstance(media_cfg, dict) or not media_cfg.get("enabled"):
        return None
    if flag is not None and not media_cfg.get(flag, default):
        return None
    return media_cfg


def _positive(media_cfg, key, default):
    value = media_cfg.get(key, default)
    if not isinstance(value, (int, float)) or value <= 0:
        raise MediaConfigurationError(f"media.{key} must be positive")
    return value


def _arr_clients(media_cfg, secrets):
    missing = [
        name
        for name in ("radarrApiKey", "sonarrApiKey")
        if not config.real_key(secrets.get(name))
    ]
    if missing:
        raise MediaConfigurationError("missing media API keys: " + ", ".join(missing))
    for name in ("radarrUrl", "sonarrUrl"):
        if not isinstance(media_cfg.get(name), str) or not media_cfg[name]:
            raise MediaConfigurationError(f"media.{name} is missing")
    return tuple(
        ArrClient(name, media_cfg[f"{name.lower()}Url"], secrets[key])
        for name, key in (("Radarr", "radarrApiKey"), ("Sonarr", "sonarrApiKey"))
    )


def servarr_clients(media_cfg, secrets):
    """Radarr and Sonarr, plus Prowlarr when it has a key."""
    clients = _arr_clients(media_cfg, secrets)
    if not config.real_key(secrets.get("prowlarrApiKey")):
        return clients
    prowlarr = ArrClient(
        "Prowlarr",
        media_cfg.get("prowlarrUrl", ""),
        secrets["prowlarrApiKey"],
        api_version="v1",
    )
    return (*clients, prowlarr)


def _optional_monitor(cfg, log, flag, what, build, default=True):
    """A monitor from the media config, or None: off by config, or refused with
    a lane_disabled line."""
    media_cfg = _media_cfg(cfg, flag, default)
    if media_cfg is None:
        return None
    try:
        return build(media_cfg)
    except MediaConfigurationError as e:
        log.warn("lane_disabled", what=what, reason=str(e))
        return None


def proton_port_monitor_from_config(cfg, secrets, log):
    return _optional_monitor(
        cfg,
        log,
        "protonPortSync",
        "proton_port_sync",
        lambda media_cfg: ProtonPortMonitor(
            qbit_from_config(media_cfg, secrets),
            log,
            poll_s=_positive(media_cfg, "pollS", 30),
            interface=str(media_cfg.get("qbittorrentNetworkInterface", "ProtonVPN")),
            exe=media_cfg.get("qbittorrentExe") or None,
        ),
        default=False,
    )


def media_health_monitor_from_config(cfg, secrets, log, operations=None):
    def build(media_cfg):
        # 0 turns the reaping off; the watch still reports stalls.
        grace = media_cfg.get("stalledGraceMinutes", 30)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace < 0:
            raise MediaConfigurationError(
                "media.stalledGraceMinutes must be a number of minutes, 0 or more"
            )
        return MediaHealthMonitor(
            _arr_clients(media_cfg, secrets),
            log,
            poll_s=_positive(media_cfg, "healthPollS", HEALTH_POLL_S),
            operations=operations,
            stall_grace_s=60 * grace,
        )

    return _optional_monitor(cfg, log, "healthSync", "media_health_sync", build)


def media_update_monitor_from_config(cfg, secrets, log):
    return _optional_monitor(
        cfg,
        log,
        "autoUpdate",
        "media_auto_update",
        lambda media_cfg: MediaUpdateMonitor(
            servarr_clients(media_cfg, secrets), log, paths.HOME / "media"
        ),
    )


def _media_root(env_path):
    """MEDIA_ROOT as Compose reads it. The file is gitignored, so a checkout
    that is not the K15 has none and the watch stays off."""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "MEDIA_ROOT":
            return value.strip() or None
    return None


def disk_health_monitor_from_config(cfg, log):
    def build(media_cfg):
        poll_s = _positive(media_cfg, "diskPollS", DISK_POLL_S)
        warn_gb = _positive(media_cfg, "diskFreeWarnGb", FREE_WARN_BYTES // GB)
        env_path = paths.HOME / "media" / ".env"
        root = _media_root(env_path)
        if not root:
            raise MediaConfigurationError(
                f"no MEDIA_ROOT in {env_path} - run Start-Media.ps1"
            )
        # The library volume fills from downloads; the checkout volume holds
        # the databases and the event log. Anchors, so one volume named twice
        # is watched once.
        mounts = sorted(
            {Path(root).anchor or root, Path(paths.HOME).anchor or str(paths.HOME)}
        )
        return DiskHealthMonitor(
            mounts, log, poll_s=poll_s, free_warn_bytes=int(warn_gb * GB)
        )

    return _optional_monitor(cfg, log, "diskWatch", "disk_watch", build)


def from_config(cfg, secrets, log):
    media_cfg = _media_cfg(cfg)
    if media_cfg is None:
        return None
    try:
        radarr, sonarr = _arr_clients(media_cfg, secrets)
        for key in ("movieRoot", "seriesRoot"):
            if not isinstance(media_cfg.get(key), str) or not media_cfg[key]:
                raise MediaConfigurationError(f"media.{key} is missing")
        for key in ("moviePresets", "seriesPresets"):
            mapping = media_cfg.get(key)
            if (
                not isinstance(mapping, dict)
                or not mapping
                or not all(isinstance(name, str) and name for name in mapping.values())
            ):
                raise MediaConfigurationError(f"media.{key} is invalid")
        _positive(media_cfg, "pollS", 30)
    except MediaConfigurationError as e:
        log.warn("lane_disabled", what="media", reason=str(e))
        return None
    # Prowlarr and qBittorrent are extras: their absence disables their tools,
    # not the media lane.
    prowlarr = qbit = None
    if config.real_key(secrets.get("prowlarrApiKey")):
        try:
            prowlarr = ArrClient(
                "Prowlarr",
                media_cfg.get("prowlarrUrl", ""),
                secrets["prowlarrApiKey"],
                api_version="v1",
            )
        except MediaConfigurationError as e:
            log("lane_disabled", what="prowlarr_tools", reason=str(e))
    else:
        log("lane_disabled", what="prowlarr_tools", reason="prowlarrApiKey missing")
    try:
        qbit = qbit_from_config(media_cfg, secrets)
    except MediaConfigurationError as e:
        log("lane_disabled", what="torrent_tools", reason=str(e))
    return MediaService(media_cfg, log, radarr, sonarr, prowlarr=prowlarr, qbit=qbit)
