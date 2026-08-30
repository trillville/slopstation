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


class FakeMedia:
    def __init__(self):
        self.result = {"complete": False, "progress": {"percent": 20},
                       "detail": "1 of 5 aired episodes are ready"}
        self.error = None
        self.search_ready = False

    def dispatch_pending_series_search(self, operation):
        if self.search_ready and bool(
                (operation.get("metadata") or {}).get("search_pending")):
            return [77]
        return []

    def observe(self, operation):
        if self.error:
            raise self.error
        return dict(self.result)


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

    steam.downloads = [{"appid": 413150, "name": "Stardew Valley",
                        "percent": 100, "paused": False, "queue": 0}]
    monitor.reconcile_once()
    assert store.get(operation["id"])["state"] == operations.RUNNING
    assert not terminal, "100% bytes without install proof became terminal"

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
    print("  monitor: progress, silent UNKNOWN, manifest completion, terminal stability")

    other = store.track_steam_install(570, "Dota 2", verified=True)
    ok, detail = store.cancel(other["id"])
    assert not ok and "not supported" in detail
    assert store.get(other["id"])["state"] == operations.RUNNING
    assert store.for_assistant("active")[0]["state"] in operations.ACTIVE
    print("  capability: unsupported Steam cancellation leaves state unchanged")

    # The second authority justifies generic creation, but keeps its concrete
    # observation semantics in MediaMonitor.
    fresh_state()
    media_terminal = []
    media_store = operations.OperationStore(log, on_terminal=media_terminal.append)
    media_op = media_store.track_external(
        "series_acquisition", "sonarr", "41", "Breaking Bad", turn="beef01",
        detail="Sonarr accepted the request",
        metadata={"catalog_id": 81189, "preset": "1080p",
                  "profile": "Series HD", "seasons": [2]})
    reused = media_store.track_external(
        "series_acquisition", "sonarr", "41", "Breaking Bad",
        detail="Sonarr accepted the request",
        metadata={"catalog_id": 81189, "preset": "2160p",
                  "profile": "Series UHD", "seasons": [2],
                  "search_pending": True})
    assert reused["id"] == media_op["id"]
    assert reused["metadata"]["preset"] == "2160p"
    fake_media = FakeMedia()
    media_monitor = operations.MediaMonitor(media_store, fake_media, log)
    assert media_monitor.reconcile_once() == 1
    assert media_store.get(media_op["id"])["progress"]["percent"] == 20
    assert media_store.get(media_op["id"])["metadata"]["search_pending"]
    fake_media.search_ready = True
    media_monitor.reconcile_once()
    assert "search_pending" not in media_store.get(media_op["id"])["metadata"]
    assert media_store.get(media_op["id"])["metadata"]["command_ids"] == [77]
    fake_media.error = RuntimeError("offline")
    media_monitor.reconcile_once()
    assert media_store.get(media_op["id"])["state"] == operations.UNKNOWN
    assert not media_terminal
    fake_media.error = None
    fake_media.result = {"complete": True, "progress": {"percent": 100},
                         "detail": "5 of 5 aired episodes are ready"}
    media_monitor.reconcile_once()
    media_done = media_store.get(media_op["id"])
    assert media_done["state"] == operations.SUCCEEDED
    assert media_done["summary"] == (
        "The requested episodes of Breaking Bad are ready to watch.")
    assert len(media_terminal) == 1
    media_monitor.reconcile_once()
    assert len(media_terminal) == 1
    ok, detail = media_store.cancel(media_op["id"])
    assert not ok and "already succeeded" in detail
    print("  media: structured policy, active dedupe, UNKNOWN silence, positive completion")

    phase_op = media_store.track_external(
        "movie_acquisition", "radarr", "51", "Arrival",
        metadata={"catalog_id": 329865, "command_ids": [9]})
    fake_media.result = {
        "complete": False,
        "progress": {"phase": "searching"},
        "detail": "Radarr is searching"}
    media_monitor.reconcile_once()
    fake_media.result = {
        "complete": False,
        "progress": {"phase": "waiting_for_match"},
        "detail": "no acceptable release yet"}
    media_monitor.reconcile_once()
    assert len(media_store.pending_notifications()) == 1
    assert media_store.pending_notifications()[0]["key"] == "waiting_for_match"
    fake_media.result = {
        "complete": False,
        "progress": {"phase": "downloading", "percent": 2},
        "detail": "download is 2% complete"}
    media_monitor.reconcile_once()
    assert {row["key"] for row in media_store.pending_notifications()} == {
        "waiting_for_match", "download_started"}
    media_monitor.reconcile_once()
    assert len(media_store.pending_notifications()) == 2
    print("  phases: waiting and download receipts are durable and edge-triggered")

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        assert operations.main(["list"]) == 0
    assert media_op["id"] in stdout.getvalue()
    print("  CLI: active operations render without a live Steam session")

    fresh_state()
    canceled_store = operations.OperationStore(log)
    canceled_op = canceled_store.track_external(
        "series_acquisition", "sonarr", "71", "Andor",
        metadata={"catalog_id": 393189, "seasons": [1]})
    canceled_media = FakeMedia()
    canceled_media.result = {
        "complete": False, "canceled": True,
        "progress": {"episodes": 0, "total_episodes": 0, "percent": 0},
        "detail": "requested episodes are unmonitored"}
    operations.MediaMonitor(canceled_store, canceled_media, log).reconcile_once()
    assert canceled_store.get(canceled_op["id"])["state"] == operations.CANCELED
    print("  media: externally unmonitored scope reconciles to cancellation")

    fresh_state()
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
    delivery_store.on_notification = ann.submit_notification
    delivery_store.notify(
        pending["id"], "download_started",
        "Team Fortress Classic has started downloading.")
    assert wait_for(lambda: not any(
        row["operation_id"] == pending["id"]
        for row in delivery_store.pending_notifications()))
    print("  announcer: interrupted delivery stays pending; full retry acknowledges once")

    print("OK - operations: persistence, reconciliation, CLI, and announcement semantics")


if __name__ == "__main__":
    main()
