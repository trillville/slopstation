"""Poll free space on volumes used by Slopstation."""

import shutil

from slopstation.agent.media.clients import _clean_text
from slopstation.agent.monitor import ChangeOnly, Monitor

DISK_POLL_S = 300
# One 2160p remux is ~70 GB, so a threshold below that reports a volume that
# is already too full to take the next grab.
FREE_WARN_BYTES = 250 * 1024**3


class DiskHealthMonitor(Monitor):
    """Report a volume running out of room before an import fails on it.

    Emits on transition only: a full disk stays full, and one line per poll
    would bury the crossing that is the news.
    """

    THREAD_NAME = "disk-health-monitor"

    def __init__(
        self, mounts, log, poll_s=DISK_POLL_S, free_warn_bytes=FREE_WARN_BYTES
    ):
        self.mounts = tuple(mounts)
        self.log = log
        self.poll_s = poll_s
        self.free_warn_bytes = free_warn_bytes
        self._low = set()
        self._failures = ChangeOnly()

    def reconcile_once(self):
        for mount in self.mounts:
            try:
                self._check(mount, shutil.disk_usage(mount))
                self._failures.cleared(mount)
            except Exception as e:
                detail = _clean_text(e)
                if self._failures.changed(mount, detail):
                    self.log.error("disk_watch_failed", mount=mount, err=detail)

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
