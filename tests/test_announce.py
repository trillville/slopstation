"""Test the announcer's delivery loop against a ledger it cannot read."""

import queue
import threading

from helpers import CapturingLog
from slopstation.agent.speech import announce


def test_an_unreadable_ledger_skips_the_bulletin_not_the_thread():
    """The store refuses a corrupt file with a raise. The loop that speaks
    bulletins has to outlive that: the row keeps its pending flag, and the
    next queued item is still looked up."""
    reads = []

    class Store:
        def pending_announcements(self):
            reads.append(1)
            if len(reads) == 1:
                raise ValueError("operations.json: not JSON")
            return []

    a = announce.Announcer.__new__(announce.Announcer)
    a.log, a.store, a.duck = CapturingLog("voice"), Store(), None
    a.session_active, a.abort = threading.Event(), threading.Event()
    a._q = queue.Queue()
    for item in (("terminal", "op1", None), ("terminal", "op2", None), None):
        a._q.put(item)
    a._run()  # returns on the None; a raise would have escaped here
    assert len(reads) == 2, reads
    failed = a.log.find("announce_failed")
    assert len(failed) == 1 and failed[0]["operation"] == "op1", failed
    assert failed[0]["fallback"] == "skipped" and "not JSON" in failed[0]["err"]
