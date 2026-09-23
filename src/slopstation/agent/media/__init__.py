"""The media stack: Radarr, Sonarr, Prowlarr, qBittorrent, Proton and the
media drive.

`MediaService` is the one object callers hold. `from_config` builds it, and
the three watch factories build the pollers that run beside it."""

from slopstation.agent.media.config import (
    disk_health_monitor_from_config,
    from_config,
    media_health_monitor_from_config,
    proton_port_monitor_from_config,
)
from slopstation.agent.media.core import Observation
from slopstation.agent.media.service import MediaService

__all__ = [
    "MediaService",
    "Observation",
    "disk_health_monitor_from_config",
    "from_config",
    "media_health_monitor_from_config",
    "proton_port_monitor_from_config",
]
