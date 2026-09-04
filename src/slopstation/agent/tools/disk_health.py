"""Poll free space on volumes used by Slopstation."""

import shutil

from slopstation import events
from slopstation.agent.tools.media_clients import _clean_text

DISK_POLL_S = 300
# One 2160p remux is ~70 GB, so a threshold below that reports a volume that
# is already too full to take the next grab.
FREE_WARN_BYTES = 250 * 1024**3


class DiskHealthMonitor:
    """Report a volume running out of room before an import fails on it.

    Emits on transition only: a full disk stays full, and one line per poll
    would bury the crossing that is the news.
    """

    def __init__(
        self, mounts, log, poll_s=DISK_POLL_S, free_warn_bytes=FREE_WARN_BYTES
    ):
        self.mounts = tuple(mounts)
        self.log = log
        self.poll_s = poll_s
        self.free_warn_bytes = free_warn_bytes
        self._low = set()
        self._last_failure = {}

    def start(self):
        events.Ticker("disk-health-monitor", self.poll_s, self.reconcile_once).start()

    def reconcile_once(self):
        for mount in self.mounts:
            try:
                self._check(mount, shutil.disk_usage(mount))
                self._last_failure[mount] = None
            except Exception as e:
                detail = _clean_text(e)
                # A vanished volume fails every poll; only the change is news.
                if detail != self._last_failure.get(mount):
                    self.log.error("disk_watch_failed", mount=mount, err=detail)
                    self._last_failure[mount] = detail

    def _check(self, mount, usage):
        free_gb = round(usage.free / 1024**3, 1)
        pct_free = round(100.0 * usage.free / usage.total, 1) if usage.total else 0.0
        if usage.free < self.free_warn_bytes:
            # No first-pass suppression: a volume already low at startup is
            # current state, not backlog.
            if mount not in self._low:
                self.log.warn(
                    "disk_space_low",
                    mount=mount,
                    free_gb=free_gb,
                    total_gb=round(usage.total / 1024**3, 1),
                    pct_free=pct_free,
                )
                self._low.add(mount)
        elif mount in self._low:
            self.log(
                "disk_space_cleared", mount=mount, free_gb=free_gb, pct_free=pct_free
            )
            self._low.discard(mount)
