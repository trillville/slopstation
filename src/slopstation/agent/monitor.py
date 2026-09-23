"""Base for the pollers: a stoppable ticker, and a raise that costs one log
line, not the thread."""

from typing import Any

from slopstation import events


class Monitor:
    THREAD_NAME = "monitor"
    log: Any
    poll_s: float
    ticker: events.Ticker | None = None

    def reconcile_once(self):
        raise NotImplementedError

    def start(self) -> events.Ticker:
        """Start polling. Returns the ticker so the owner can watch it."""
        self.ticker = events.Ticker(self.THREAD_NAME, self.poll_s, self._tick)
        self.ticker.start()
        return self.ticker

    def stop(self):
        if self.ticker is not None:
            self.ticker.stop.set()

    def _tick(self):
        try:
            self.reconcile_once()
        except Exception as e:
            self.log.error("operation_monitor_failed", err=str(e))


class ChangeOnly:
    """Failures already reported, by key. An outage logs once when it starts
    and once when it changes, not once per poll."""

    def __init__(self):
        self._last: dict = {}

    def changed(self, key, detail) -> bool:
        """True when `detail` is new for `key`; records it either way."""
        if detail == self._last.get(key):
            return False
        self._last[key] = detail
        return True

    def cleared(self, key) -> None:
        self._last[key] = None
