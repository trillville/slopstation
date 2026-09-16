"""One base for the pollers: a stoppable ticker, and a raise that costs one
log line rather than the thread."""

from typing import Any

from slopstation import events


class Monitor:
    THREAD_NAME = "monitor"
    log: Any
    poll_s: float
    ticker: events.Ticker | None = None

    def reconcile_once(self):
        raise NotImplementedError

    def start(self):
        self.ticker = events.Ticker(self.THREAD_NAME, self.poll_s, self._tick)
        self.ticker.start()

    def stop(self):
        if self.ticker is not None:
            self.ticker.stop.set()

    def _tick(self):
        try:
            self.reconcile_once()
        except Exception as e:
            self.log.error("operation_monitor_failed", err=str(e))


class ChangeOnly:
    """Which failures a poller has already reported, keyed by what failed. An
    outage is one line when it starts and one when it changes, not one line
    per poll until someone notices."""

    def __init__(self):
        self._last: dict = {}

    def changed(self, key, detail) -> bool:
        """True when `detail` is news for `key`; records it either way."""
        if detail == self._last.get(key):
            return False
        self._last[key] = detail
        return True

    def cleared(self, key) -> None:
        self._last[key] = None
