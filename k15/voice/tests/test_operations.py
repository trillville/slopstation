"""Blind test: durable operation state, Steam reconciliation, and delivery."""
import contextlib
import io
import sys
import time
import types

import _bootstrap  # noqa: F401
from _bootstrap import fresh_state

# Announcer's queue/store contract needs no Pipecat or audio hardware.
audio_stub = types.ModuleType("audio")
audio_stub.resolve_device = lambda *a, **kw: None
earcons_stub = types.ModuleType("earcons")
earcons_stub.SAMPLE_RATE = 16000
earcons_stub.pcm = lambda name: b"earcon"
sys.modules["audio"] = audio_stub
sys.modules["earcons"] = earcons_stub

import announce
import cglib
import operations


def wait_for(predicate, timeout=2):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeSteam:
    def __init__(self):
        self.online = True
        self.downloads = []

    def client_online(self):
        return self.online

    def download_status(self):
        return list(self.downloads)


def main():
    fresh_state()
    log = cglib.CapturingLog("voice")
    terminal = []
    store = operations.OperationStore(log, on_terminal=terminal.append)

    operation = store.track_steam_install(413150, "Stardew Valley",
                                          turn="4c1d0e", verified=False)
    assert operation["state"] == operations.QUEUED
    same = store.track_steam_install(413150, "Stardew Valley",
                                     turn="another", verified=True)
    assert same["id"] == operation["id"] and same["state"] == operations.RUNNING
    assert same["last_observed"] and log.find("operation_observed")
    reloaded = operations.OperationStore(log)
    assert reloaded.get(operation["id"])["turn"] == "4c1d0e"
    print("  store: atomic persistence and active appid deduplication")

    steam = FakeSteam()
    installed = set()
    monitor = operations.SteamMonitor(store, steam, log,
                                      installed_probe=lambda: installed)
    steam.downloads = [{"appid": 413150, "name": "Stardew Valley",
                        "percent": 25, "paused": False, "queue": 0}]
    assert monitor.reconcile_once() == 1
    observed = store.get(operation["id"])
    assert observed["state"] == operations.RUNNING
    assert observed["progress"]["percent"] == 25

    steam.online = False
    monitor.reconcile_once()
    assert store.get(operation["id"])["state"] == operations.UNKNOWN
    assert not terminal and not store.pending_announcements()

    steam.online = True
    steam.downloads = []
    monitor.reconcile_once()
    assert store.get(operation["id"])["state"] == operations.UNKNOWN
    assert not terminal, "absence without install proof became terminal"

    installed.add(413150)
    monitor.reconcile_once()
    finished = store.get(operation["id"])
    assert finished["state"] == operations.SUCCEEDED
    assert finished["announcement_pending"] and len(terminal) == 1
    monitor.reconcile_once()
    assert len(terminal) == 1, "terminal polling duplicated the callback"
    assert store.observe(operation["id"], operations.UNKNOWN)["state"] == operations.SUCCEEDED
    store.for_assistant("recent", acknowledge=True)
    heard = store.get(operation["id"])
    assert not heard["announcement_pending"] and heard["delivered"]
    print("  monitor: progress, silent UNKNOWN, positive completion, terminal stability")

    other = store.track_steam_install(570, "Dota 2", verified=True)
    ok, detail = store.cancel(other["id"])
    assert not ok and "not supported" in detail
    assert store.get(other["id"])["state"] == operations.RUNNING
    assert store.for_assistant("active")[0]["state"] in operations.ACTIVE
    print("  capability: unsupported Steam cancellation leaves state unchanged")

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        assert operations.main(["list", "--active"]) == 0
    assert other["id"] in stdout.getvalue()
    print("  CLI: active operations render without a live Steam session")

    delivery_log = cglib.CapturingLog("voice")
    delivery_store = operations.OperationStore(delivery_log)
    voice = dict(_bootstrap.CONFIG["voice"])
    ann = announce.Announcer(voice, {"deepgramApiKey": "x" * 40}, delivery_log)
    ann.store = delivery_store
    delivery_store.on_terminal = ann.submit
    announce.synth = lambda *a, **kw: b"speech"
    ann._play = lambda pcm: False
    pending = delivery_store.track_steam_install(20, "Team Fortress Classic",
                                                 verified=True)
    delivery_store.observe(pending["id"], operations.SUCCEEDED,
                           {"percent": 100}, "fully installed")
    assert wait_for(lambda: bool(delivery_log.find("announce_cut_short")))
    assert delivery_store.get(pending["id"])["announcement_pending"]
    assert not ann.follow_up.is_set()

    ann._play = lambda pcm: True
    ann.submit(delivery_store.get(pending["id"]))
    assert wait_for(lambda: not delivery_store.get(pending["id"])[
        "announcement_pending"])
    assert ann.follow_up.is_set()
    assert len(delivery_log.find("operation_announced")) == 1
    print("  announcer: interrupted delivery stays pending; full retry acknowledges once")

    print("OK - operations: persistence, reconciliation, CLI, and announcement semantics")


if __name__ == "__main__":
    main()
