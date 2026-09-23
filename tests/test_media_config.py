"""Building the media service and its watches from config.json, and what
each refuses."""

import json

import pytest

import helpers
from helpers import CapturingLog
from slopstation.agent import media


@pytest.mark.parametrize(
    "factory, flag",
    [
        (
            lambda cfg, log: media.media_health_monitor_from_config(cfg, {}, log),
            "healthSync",
        ),
        (lambda cfg, log: media.disk_health_monitor_from_config(cfg, log), "diskWatch"),
    ],
)
def test_a_watch_is_off_when_config_says_so(factory, flag):
    cfg = {"media": {"enabled": True, flag: False}}
    assert factory(cfg, CapturingLog("voice")) is None


def test_health_watch_refuses_a_bad_stall_grace_without_falling_over():
    """A bad value disables the watch with a warning the doctor shows, the
    way every other media key does, rather than taking the lane down."""
    cfg = {"media": {"enabled": True, "stalledGraceMinutes": "soon"}}
    log = CapturingLog("voice")
    assert media.media_health_monitor_from_config(cfg, {}, log) is None
    assert "stalledGraceMinutes" in log.find("lane_disabled")[-1]["reason"]


def test_health_watch_refuses_a_missing_arr_url_the_same_way():
    """A missing URL is a MediaConfigurationError from the shared client
    builder; the watch factories catch nothing else."""
    cfg = {"media": {"enabled": True, "sonarrUrl": "http://s"}}
    secrets = {"radarrApiKey": "k" * 32, "sonarrApiKey": "k" * 32}
    log = CapturingLog("voice")
    assert media.media_health_monitor_from_config(cfg, secrets, log) is None
    assert log.find("lane_disabled")[-1]["reason"] == "media.radarrUrl is missing"


def test_disk_watch_needs_a_host_root():
    # No media/.env in the runtime home means no host root to resolve: a
    # checkout that is not the K15 runs the supervisor without inventing a
    # volume to watch.
    cfg = {"media": {"enabled": True}}
    assert media.disk_health_monitor_from_config(cfg, CapturingLog("voice")) is None


# --- factory gating -----------------------------------------------------------


def test_from_config_needs_the_lane_and_its_keys():
    cfg = json.loads(json.dumps(helpers.CONFIG))
    log = CapturingLog("voice")
    assert media.from_config(cfg, {}, log) is None
    cfg["media"]["enabled"] = True
    assert media.from_config(cfg, {}, log) is None
    assert log.find("lane_disabled")[-1]["what"] == "media"
    # With the two arr keys the lane is up; Prowlarr and qBittorrent are
    # extras whose absence disables only their own tools.
    keys = {"radarrApiKey": "r" * 32, "sonarrApiKey": "s" * 32}
    svc = media.from_config(cfg, keys, log)
    assert svc is not None and svc.prowlarr is None and svc.qbit is None
    assert {r["what"] for r in log.find("lane_disabled")} >= {
        "prowlarr_tools",
        "torrent_tools",
    }
    full = media.from_config(
        cfg,
        {**keys, "prowlarrApiKey": "p" * 32, "qbittorrentPassword": "q" * 16},
        log,
    )
    assert full.prowlarr.api_version == "v1" and full.qbit is not None
