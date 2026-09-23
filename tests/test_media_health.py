"""The watches that poll the stack: the apps' health, stalled and empty
grabs, grabs nobody asked for, and free space on the disk."""

import collections
import dataclasses

from helpers import CapturingLog, FakeArr
from slopstation.agent.media import disk, health

# --- media health watch -------------------------------------------------------


def test_health_watch_reports_transitions_once():
    watch_radarr = FakeArr(
        "Radarr",
        health=[
            {
                "source": "IndexerStatusCheck",
                "type": "warning",
                "message": "Indexers unavailable due to failures",
            }
        ],
        history={
            "records": [
                {"id": 4, "eventType": "grabbed", "sourceTitle": "Dune.2021"},
                {
                    "id": 5,
                    "eventType": "downloadFailed",
                    "sourceTitle": "Dune.2021",
                    "data": {"message": "Torrent removed by qBittorrent"},
                },
            ]
        },
    )
    watch_sonarr = FakeArr(
        "Sonarr",
        queue={
            "records": [
                {
                    "id": 1,
                    "downloadId": "ABC",
                    "title": "Show.S01",
                    "trackedDownloadStatus": "warning",
                    "statusMessages": [{"messages": ["Not a preferred word upgrade"]}],
                },
                {
                    "id": 2,
                    "downloadId": "ABC",
                    "title": "Show.S01",
                    "trackedDownloadStatus": "warning",
                    "statusMessages": [{"messages": ["Not a preferred word upgrade"]}],
                },
            ]
        },
    )
    watch_log = CapturingLog("voice")
    watch = health.MediaHealthMonitor((watch_radarr, watch_sonarr), watch_log)

    watch.reconcile_once()
    issue = watch_log.find("media_health_issue")
    assert len(issue) == 1 and issue[0]["source"] == "IndexerStatusCheck"
    assert issue[0]["level"] == "warn" and issue[0]["app"] == "Radarr"
    # History already on disk at startup is backlog, not news.
    assert not watch_log.find("media_import_failed")
    # A season pack is one queue row per episode and one thing to act on.
    stalled = watch_log.find("media_queue_stalled")
    assert len(stalled) == 1 and stalled[0]["download"] == "ABC"

    watch_log.records.clear()
    watch.reconcile_once()
    assert watch_log.events() == []

    watch_log.records.clear()
    watch_radarr.set(health=[])
    watch_radarr.history["records"].extend(
        [
            {
                "id": 6,
                "eventType": "importFailed",
                "sourceTitle": "Heat.1995",
                "downloadId": "PACK",
                "episodeId": 1,
                "data": {"message": "No files found are eligible for import"},
            },
            {
                "id": 7,
                "eventType": "importFailed",
                "sourceTitle": "Heat.1995",
                "downloadId": "PACK",
                "episodeId": 2,
                "data": {"message": "No files found are eligible for import"},
            },
        ]
    )
    watch_sonarr.queue["records"][0]["trackedDownloadStatus"] = "error"
    watch.reconcile_once()
    failed = watch_log.find("media_import_failed")
    # One bad grab is one line even though it failed once per episode.
    assert [r["title"] for r in failed] == ["Heat.1995"]
    assert failed[0]["records"] == 2
    assert failed[0]["level"] == "error" and failed[0]["kind"] == "importFailed"
    assert watch_log.find("media_health_cleared")[0]["source"] == "IndexerStatusCheck"
    assert watch_log.find("media_queue_stalled")[0]["status"] == "error"


def test_health_watch_reaps_a_grab_that_never_starts():
    """Nothing received for the grace period gets blocklisted so the app
    takes its next candidate; a download that is moving, or complete and
    waiting to import, is left alone; a target is given up on after
    REAP_LIMIT replacements."""
    clock = [1000.0]
    dead = {
        "id": 1,
        "downloadId": "DEAD",
        "episodeId": 7,
        "title": "Show.S01E01.1080p",
        "status": "warning",
        "size": 100.0,
        "sizeleft": 100.0,
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"messages": ["stalled with no connections"]}],
    }
    magnet = {
        "id": 2,
        "downloadId": "META",
        "episodeId": 8,
        "title": "Show.S01E02.720p",
        "status": "queued",
        "size": 0.0,
        "sizeleft": 0.0,
        "trackedDownloadStatus": "ok",
        "statusMessages": [{"messages": ["qBittorrent is downloading metadata"]}],
    }
    moving = {"id": 3, "downloadId": "MOVING", "status": "downloading"}
    moving.update(size=100.0, sizeleft=40.0)
    done = {"id": 4, "downloadId": "DONE", "status": "completed"}
    done.update(size=100.0, sizeleft=0.0)
    # Idle for a reason that is not the release: not dead.
    paused = {**dead, "id": 5, "downloadId": "PAUSED", "status": "paused"}
    held = {**dead, "id": 6, "downloadId": "", "status": "delay", "size": 0.0}
    away = {
        **dead,
        "id": 7,
        "downloadId": "AWAY",
        "status": "downloadClientUnavailable",
    }
    reap_sonarr = FakeArr(
        "Sonarr",
        queue={"records": [dead, magnet, moving, done, paused, held, away]},
    )
    reap_log = CapturingLog("voice")
    watch = health.MediaHealthMonitor(
        (reap_sonarr,), reap_log, stall_grace_s=1800, now=lambda: clock[0]
    )
    watch.reconcile_once()
    clock[0] += 1799
    watch.reconcile_once()
    assert not reap_sonarr.deletes
    clock[0] += 1
    watch.reconcile_once()
    blocklist = {"removeFromClient": "true", "blocklist": "true"}
    assert reap_sonarr.deletes == [("queue/1", blocklist), ("queue/2", blocklist)]
    reaped = reap_log.find("media_queue_reaped")
    assert [(r["download"], r["idle_s"], r["attempt"]) for r in reaped] == [
        ("DEAD", 1800, 1),
        ("META", 1800, 1),
    ]
    # The app grabs the next copy for the same episode: a fresh clock, and
    # the target's count carries on until the limit.
    for n, hash_ in enumerate(("DEAD2", "DEAD3", "DEAD4"), start=2):
        reap_sonarr.queue["records"] = [{**dead, "id": 10 + n, "downloadId": hash_}]
        watch.reconcile_once()
        clock[0] += 1800
        watch.reconcile_once()
    assert [r["attempt"] for r in reap_log.find("media_queue_reaped")][2:] == [2, 3]
    assert len(reap_sonarr.deletes) == 4, "the fourth copy is left where it is"
    failed = reap_log.find("media_queue_reap_failed")
    assert len(failed) == 1 and "3 dead grabs" in failed[0]["err"]
    clock[0] += 1800
    watch.reconcile_once()
    assert len(reap_log.find("media_queue_reap_failed")) == 1, "said once"
    # Off means off; the stall is still reported.
    off_sonarr = FakeArr("Sonarr", queue={"records": [dict(dead)]})
    off_log = CapturingLog("voice")
    off = health.MediaHealthMonitor(
        (off_sonarr,), off_log, stall_grace_s=0, now=lambda: clock[0]
    )
    off.reconcile_once()
    clock[0] += 10**6
    off.reconcile_once()
    assert not off_sonarr.deletes and off_log.find("media_queue_stalled")


def test_health_watch_reaps_a_download_that_completed_empty():
    """qBittorrent's excluded-file-names filter deselects the executable in a
    fake release, so the download finishes having transferred nothing and the
    app reports no eligible files rather than the executable verdict. Final on
    sight, like the executable verdict; a real completed grab still waits."""
    clock = [1000.0]
    empty = {
        "id": 1,
        "downloadId": "EMPTY",
        "episodeId": 435,
        "title": "Show.S18E07.1080p.WEB.h264-FAKE.exe",
        "status": "completed",
        "size": 0.0,
        "sizeleft": 0.0,
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [
            {"messages": ["No files found are eligible for import in /data/torrents"]}
        ],
    }
    importing = {
        "id": 2,
        "downloadId": "REAL",
        "episodeId": 436,
        "status": "completed",
        "size": 100.0,
        "sizeleft": 0.0,
    }
    sonarr = FakeArr("Sonarr", queue={"records": [empty, importing]})
    log = CapturingLog("voice")
    watch = health.MediaHealthMonitor(
        (sonarr,), log, stall_grace_s=1800, now=lambda: clock[0]
    )
    watch.reconcile_once()
    blocklist = {"removeFromClient": "true", "blocklist": "true"}
    assert sonarr.deletes == [("queue/1", blocklist)]
    reaped = log.find("media_queue_reaped")
    assert [(r["download"], r["reason"], r["idle_s"]) for r in reaped] == [
        ("EMPTY", "empty", 0)
    ]


# --- grabs no operation asked for ---------------------------------------------


@dataclasses.dataclass
class FakeLedger:
    rows: list

    def active(self, kind=None):
        return list(self.rows)


def test_unattributed_grabs_are_reported_per_download():
    grab_sonarr = FakeArr("Sonarr")
    watch_log = CapturingLog("voice")
    grabs = health.MediaHealthMonitor(
        (grab_sonarr,),
        watch_log,
        operations=FakeLedger([{"authority": "sonarr", "external_ref": "3"}]),
    )
    grabs.reconcile_once()
    grab_sonarr.history["records"].extend(
        [
            {
                "id": 20,
                "eventType": "grabbed",
                "seriesId": 3,
                "downloadId": "MINE",
                "sourceTitle": "Asked.For.S05",
                "data": {"indexer": "1337x"},
            },
            {
                "id": 21,
                "eventType": "grabbed",
                "seriesId": 9,
                "downloadId": "LOOSE",
                "sourceTitle": "Nobody.Asked.S01",
                "data": {"indexer": "1337x"},
            },
            {
                "id": 22,
                "eventType": "grabbed",
                "seriesId": 9,
                "downloadId": "LOOSE",
                "sourceTitle": "Nobody.Asked.S01",
                "data": {"indexer": "1337x"},
            },
        ]
    )
    grabs.reconcile_once()
    loose = watch_log.find("media_grab_unattributed")
    # The owned grab stays silent; the season pack is one line, not two.
    assert [r["title"] for r in loose] == ["Nobody.Asked.S01"]
    assert loose[0]["records"] == 2 and loose[0]["indexer"] == "1337x"
    assert loose[0]["level"] == "info" and loose[0]["app"] == "Sonarr"


def test_no_ledger_means_no_attribution():
    grab_sonarr = FakeArr("Sonarr")
    watch_log = CapturingLog("voice")
    blind = health.MediaHealthMonitor((grab_sonarr,), watch_log)
    blind.reconcile_once()
    grab_sonarr.history["records"].append(
        {
            "id": 23,
            "eventType": "grabbed",
            "seriesId": 9,
            "downloadId": "Z",
            "sourceTitle": "Still.Nobody.S01",
            "data": {"indexer": "1337x"},
        }
    )
    blind.reconcile_once()
    # No ledger means no attribution, so the row stays quiet rather than
    # calling every grab unattributed.
    assert not watch_log.find("media_grab_unattributed")


# --- disk watch ---------------------------------------------------------------

GB = 1024**3
Usage = collections.namedtuple("Usage", "total free")


def test_disk_watch_reports_crossings(monkeypatch):
    table = {"M:": Usage(1000 * GB, 100 * GB), "C:": Usage(1000 * GB, 900 * GB)}

    def disk_usage(mount):
        if isinstance(table[mount], Exception):
            raise table[mount]
        return table[mount]

    monkeypatch.setattr(disk.shutil, "disk_usage", disk_usage)
    disk_log = CapturingLog("voice")
    watch = disk.DiskHealthMonitor(("M:", "C:"), disk_log, free_warn_bytes=250 * GB)
    watch.reconcile_once()
    low = disk_log.find("disk_space_low")
    # The roomy volume is silent; only the one below the threshold reports.
    assert len(low) == 1 and low[0]["mount"] == "M:"
    assert low[0]["free_gb"] == 100.0 and low[0]["pct_free"] == 10.0
    assert low[0]["level"] == "warn"
    watch.reconcile_once()
    # A full disk stays full: the crossing is the news, not the state.
    assert len(disk_log.find("disk_space_low")) == 1

    table["M:"] = Usage(1000 * GB, 600 * GB)
    watch.reconcile_once()
    assert len(disk_log.find("disk_space_cleared")) == 1
    table["M:"] = Usage(1000 * GB, 100 * GB)
    watch.reconcile_once()
    # Cleared re-arms, or a drive that oscillates would report once ever.
    assert len(disk_log.find("disk_space_low")) == 2

    disk_log.records.clear()
    table["M:"] = OSError("the device is not ready")
    watch.reconcile_once()
    watch.reconcile_once()
    # An unplugged enclosure is one line, not one line per poll.
    failed = disk_log.find("disk_watch_failed")
    assert len(failed) == 1 and failed[0]["mount"] == "M:"
