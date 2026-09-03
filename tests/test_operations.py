"""Blind test: durable operation state, Steam reconciliation, and delivery."""
import contextlib
import io
import sys
import threading
import time
import types

import helpers
from helpers import fresh_state

# Announcer's queue/store contract needs no Pipecat or audio hardware. Keyed on
# the DOTTED names: announce does `from agent.speech import audio`, so a bare
# "audio" entry intercepts nothing and the real module loads instead.
audio_stub = types.ModuleType("agent.speech.audio")
audio_stub.resolve_device = lambda *a, **kw: None
earcons_stub = types.ModuleType("agent.speech.earcons")
earcons_stub.SAMPLE_RATE = 16000
earcons_stub.pcm = lambda name: b"earcon"
sys.modules["agent.speech.audio"] = audio_stub
sys.modules["agent.speech.earcons"] = earcons_stub

from slopstation.agent.speech import announce
from slopstation import cglib
from slopstation.agent.tools import operations
from slopstation.agent.tools import operations_monitors


def _try(failures, fn, *args):
    try:
        fn(*args)
    except Exception as e:
        failures.append(repr(e))


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
        self.search_available_now = True
        self.retry_command_ids = [88]
        self.retry_error = None
        self.retries = []
        self.abandoned = []
        self.abandon_result = {"have": 80,
                               "missing": [{"season": 1, "episodes": 11}]}

    def dispatch_pending_series_search(self, operation):
        if self.search_ready and bool(
                (operation.get("metadata") or {}).get("search_pending")):
            return [77]
        return []

    def observe(self, operation):
        if self.error:
            raise self.error
        return dict(self.result)

    def search_available(self, operation):
        return self.search_available_now

    def retry_search(self, operation):
        self.retries.append(operation["id"])
        if self.retry_error:
            raise self.retry_error
        return list(self.retry_command_ids)

    def abandon_missing(self, operation):
        self.abandoned.append(operation["id"])
        return dict(self.abandon_result)


def test_operations():
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

    # Separate stores are what the agent and the CLIs actually hold, so the
    # load/mutate/write pair has to serialise on the file, not per instance.
    shared = cglib.STATE / "concurrent.json"
    writers = [operations.OperationStore(log, path=shared) for _ in range(4)]
    failures, reading = [], threading.Event()
    threads = [threading.Thread(target=lambda w=w, k=k: [
        _try(failures, w.track_steam_install, 1000 + k * 50 + i, f"g{k}{i}")
        for i in range(25)]) for k, w in enumerate(writers)]
    # A reader holding the file open denies write_json's replace on Windows,
    # so reads take the guard too.
    readers = [threading.Thread(target=lambda: [writers[0].all()
                                                for _ in iter(reading.is_set, True)],
                                daemon=True) for _ in range(2)]
    [r.start() for r in readers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    reading.set()
    assert not failures, failures[:3]
    assert len(writers[0].all()) == 100, len(writers[0].all())
    print("  store: atomic persistence, appid dedupe, concurrent readers/writers")

    steam = FakeSteam()
    installed = set()
    monitor = operations_monitors.SteamMonitor(store, steam, log,
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
    media_monitor = operations_monitors.MediaMonitor(media_store, fake_media, log)
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
    assert "search_retry_pending" not in media_store.get(
        phase_op["id"])["metadata"]
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

    fresh_state()
    retry_store = operations.OperationStore(log)
    retry_op = retry_store.track_external(
        "movie_acquisition", "radarr", "61", "Heat",
        metadata={"catalog_id": 949, "command_ids": [10]})
    retry_store.observe(retry_op["id"], operations.RUNNING,
                        {"phase": "searching"}, "Radarr is searching")
    retry_media = FakeMedia()
    retry_media.search_available_now = False
    retry_media.result = {
        "complete": False,
        "progress": {"phase": "waiting_for_match"},
        "detail": "no acceptable release yet"}
    retry_monitor = operations_monitors.MediaMonitor(retry_store, retry_media, log)
    retry_monitor.reconcile_once(now=1000)
    scheduled = retry_store.get(retry_op["id"])
    assert scheduled["metadata"]["search_retry_pending"]
    assert scheduled["metadata"]["search_retry_after"] == 1300
    assert not retry_media.retries

    retry_media.search_available_now = True
    operations_monitors.MediaMonitor(retry_store, retry_media, log).reconcile_once(now=1299)
    assert not retry_media.retries
    operations_monitors.MediaMonitor(retry_store, retry_media, log).reconcile_once(now=1300)
    retried = retry_store.get(retry_op["id"])
    assert retry_media.retries == [retry_op["id"]]
    assert retried["metadata"]["search_retry_count"] == 1
    assert retried["metadata"]["command_ids"] == [88]
    assert "search_retry_pending" not in retried["metadata"]

    retry_store.update_metadata(
        retry_op["id"], {"search_retry_count": 3},
        remove=("search_retry_pending", "search_retry_after"))
    retry_store.observe(retry_op["id"], operations.RUNNING,
                        {"phase": "searching"}, "Radarr is searching")
    retry_media.search_available_now = False
    retry_monitor.reconcile_once(now=2000)
    exhausted = retry_store.get(retry_op["id"])["metadata"]
    assert exhausted["search_retry_exhausted"]
    assert "search_retry_pending" not in exhausted

    failed_op = retry_store.track_external(
        "movie_acquisition", "radarr", "62", "Collateral",
        metadata={"catalog_id": 1538, "command_ids": [11],
                  "search_retry_pending": True,
                  "search_retry_after": 2000})
    retry_store.observe(failed_op["id"], operations.RUNNING,
                        {"phase": "waiting_for_match"}, "waiting")
    retry_media.search_available_now = True
    retry_media.retry_error = RuntimeError("search submit failed")
    retry_monitor.reconcile_once(now=2000)
    failed = retry_store.get(failed_op["id"])
    assert failed["state"] == operations.UNKNOWN
    assert failed["metadata"]["search_retry_count"] == 1
    assert failed["metadata"]["search_retry_after"] == 3800
    assert failed["metadata"]["search_retry_pending"]
    print("  retry: durable recovery gate, minimum backoff, and attempt bound")

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        assert operations.main(["list"]) == 0
    assert retry_op["id"] in stdout.getvalue()
    print("  CLI: active operations render without a live Steam session")

    fresh_state()
    give_store = operations.OperationStore(log)
    give_media = FakeMedia()
    waiting = {"complete": False,
               "progress": {"phase": "waiting_for_match", "episodes": 80,
                            "total_episodes": 91, "percent": 88},
               "detail": "no acceptable episode release is available yet"}
    give_media.result = dict(waiting)
    give_op = give_store.track_external(
        "series_acquisition", "sonarr", "71", "Rick and Morty",
        metadata={"catalog_id": 275274, "seasons": None})
    give_monitor = operations_monitors.MediaMonitor(give_store, give_media, log)
    give_monitor.reconcile_once(now=5000)
    assert give_store.get(give_op["id"])["metadata"]["waiting_since"] == 5000
    assert not give_media.abandoned
    # The heads-up speaks while the user still has context: what is missing
    # and that the close is coming.
    heads_up = [item for item in give_store.pending_notifications()
                if item["operation_id"] == give_op["id"]]
    assert len(heads_up) == 1 and heads_up[0]["key"] == "waiting_for_match"
    assert "11" in heads_up[0]["summary"]
    give_media.result = {"complete": False,
                         "progress": {"phase": "downloading",
                                      "total_episodes": 91},
                         "detail": "download is active"}
    give_monitor.reconcile_once(now=6000)   # a grab resets the clock
    assert "waiting_since" not in give_store.get(give_op["id"])["metadata"]
    give_media.result = dict(waiting)
    give_monitor.reconcile_once(now=7000)
    give_monitor.reconcile_once(now=7000 + 24 * 3600 - 1)
    assert not give_media.abandoned
    assert give_store.get(give_op["id"])["state"] == operations.RUNNING
    give_monitor.reconcile_once(now=7000 + 24 * 3600)
    closed = give_store.get(give_op["id"])
    assert give_media.abandoned == [give_op["id"]]
    assert closed["state"] == operations.SUCCEEDED
    # The close is silent - the heads-up already said it was coming.
    assert not closed["announcement_pending"]
    assert "season 1" in closed["summary"]

    unaired = give_store.track_external(
        "series_acquisition", "sonarr", "72", "Andor",
        metadata={"catalog_id": 393189, "seasons": [2]})
    give_media.result = {"complete": False,
                         "progress": {"phase": "waiting_for_match",
                                      "episodes": 0, "total_episodes": 0,
                                      "percent": 0},
                         "detail": "no requested monitored episodes have aired yet"}
    give_monitor.reconcile_once(now=8000)
    give_monitor.reconcile_once(now=8000 + 48 * 3600)
    fresh = give_store.get(unaired["id"])
    assert fresh["state"] == operations.RUNNING
    assert "waiting_since" not in fresh["metadata"]
    assert give_media.abandoned == [give_op["id"]]
    # Pre-air waiting is not "couldn't find": no heads-up either.
    assert not [item for item in give_store.pending_notifications()
                if item["operation_id"] == unaired["id"]]

    empty = give_store.track_external(
        "movie_acquisition", "radarr", "73", "Obscure Film",
        metadata={"catalog_id": 999})
    give_media.result = {"complete": False,
                         "progress": {"phase": "waiting_for_match",
                                      "percent": 0},
                         "detail": "no acceptable movie release is available yet"}
    give_media.abandon_result = {"have": 0, "missing": []}
    give_monitor.reconcile_once(now=9000)
    give_store.update_metadata(empty["id"], {
        "search_retry_pending": True,
        "search_retry_after": 9000 + 200 * 3600})
    give_monitor.reconcile_once(now=9000 + 48 * 3600)  # retry gate holds
    assert give_store.get(empty["id"])["state"] == operations.RUNNING
    give_store.update_metadata(empty["id"],
                               remove=("search_retry_pending",
                                       "search_retry_after"))
    give_monitor.reconcile_once(now=9000 + 96 * 3600)
    failed_empty = give_store.get(empty["id"])
    assert failed_empty["state"] == operations.FAILED
    assert not failed_empty["announcement_pending"]
    assert failed_empty["summary"].startswith("No acceptable release")
    print("  give up: heads-up speaks early with the deadline; the close "
          "itself is silent; unaired and retry-pending are spared")

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
    operations_monitors.MediaMonitor(canceled_store, canceled_media, log).reconcile_once()
    assert canceled_store.get(canceled_op["id"])["state"] == operations.CANCELED
    print("  media: externally unmonitored scope reconciles to cancellation")

    fresh_state()
    delivery_log = cglib.CapturingLog("voice")
    delivery_store = operations.OperationStore(delivery_log)
    voice = dict(helpers.CONFIG["voice"])
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
