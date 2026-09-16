"""One base for the pollers: a stoppable ticker, and a raise that costs one
log line rather than the thread."""

from typing import Any

from slopstation import events


class Monitor:
    THREAD_NAME = "monitor"
    FAIL_EVENT = "operation_monitor_failed"
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
            self.log.error(self.FAIL_EVENT, err=str(e))
