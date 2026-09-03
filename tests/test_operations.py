"""Durable operation state, Steam reconciliation, and delivery."""

import contextlib
import dataclasses
import io
import threading
import time
from typing import Any

import pytest

import helpers
from helpers import CapturingLog
from slopstation import paths
from slopstation.agent.speech import announce
from slopstation.agent.tools import operations, operations_monitors


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


class _Double:
    """A test double whose knobs are turned by name; a typo is a failure, not
    a new attribute."""

    def set(self, **fields):
        for name, value in fields.items():
            getattr(self, name)
            setattr(self, name, value)
        return self


@dataclasses.dataclass
class FakeSteam(_Double):
    online: bool = True
    downloads: list = dataclasses.field(default_factory=list)

    def client_online(self):
        return self.online

    def download_status(self):
        return list(self.downloads)


WAITING = {
    "complete": False,
    "progress": {
        "phase": "waiting_for_match",
        "episodes": 80,
        "total_episodes": 91,
        "percent": 88,
    },
    "detail": "no acceptable episode release is available yet",
}


@dataclasses.dataclass
class FakeMedia(_Double):
    result: dict = dataclasses.field(
        default_factory=lambda: {
            "complete": False,
            "progress": {"percent": 20},
            "detail": "1 of 5 aired episodes are ready",
        }
    )
    error: Any = None
    search_ready: bool = False
    search_available_now: bool = True
    retry_command_ids: list = dataclasses.field(default_factory=lambda: [88])
    retry_error: Any = None
    retries: list = dataclasses.field(default_factory=list)
    abandoned: list = dataclasses.field(default_factory=list)
    abandon_result: dict = dataclasses.field(
        default_factory=lambda: {"have": 80, "missing": [{"season": 1, "episodes": 11}]}
    )

    def dispatch_pending_series_search(self, operation):
        if self.search_ready and bool(
            (operation.get("metadata") or {}).get("search_pending")
        ):
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


@pytest.fixture
def log():
    return CapturingLog("voice")


def _stardew(store):
    """The Steam install the store tests share: queued by one turn, then
    confirmed running by a second request for the same game."""
    queued = store.track_steam_install(
        413150, "Stardew Valley", turn="4c1d0e", verified=False
    )
    same = store.track_steam_install(
        413150, "Stardew Valley", turn="another", verified=True
    )
    return queued, same


def _movie(store, ref, title, catalog_id, command_id, **metadata):
    return store.track_external(
        "movie_acquisition",
        "radarr",
        ref,
        title,
        metadata={"catalog_id": catalog_id, "command_ids": [command_id], **metadata},
    )


def test_store_tracks_and_reloads(log):
    store = operations.OperationStore(log)
    operation, same = _stardew(store)
    assert operation["state"] == operations.QUEUED
    assert same["id"] == operation["id"] and same["state"] == operations.RUNNING
    assert same["last_observed"] and log.find("operation_observed")
    reloaded = operations.OperationStore(log)
    assert reloaded.get(operation["id"])["turn"] == "4c1d0e"


def test_store_serialises_on_the_file(log):
    # Separate stores are what the agent and the CLIs actually hold, so the
    # load/mutate/write pair has to serialise on the file, not per instance.
    shared = paths.state() / "concurrent.json"
    writers = [operations.OperationStore(log, path=shared) for _ in range(4)]
    failures, reading = [], threading.Event()
    threads = [
        threading.Thread(
            target=lambda w=w, k=k: [
                _try(failures, w.track_steam_install, 1000 + k * 50 + i, f"g{k}{i}")
                for i in range(25)
            ]
        )
        for k, w in enumerate(writers)
    ]
    # A reader holding the file open denies write_json's replace on Windows,
    # so reads take the guard too.
    readers = [
        threading.Thread(
            target=lambda: [writers[0].all() for _ in iter(reading.is_set, True)],
            daemon=True,
        )
        for _ in range(2)
    ]
    [r.start() for r in readers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    reading.set()
    assert not failures, failures[:3]
    assert len(writers[0].all()) == 100, len(writers[0].all())


def test_steam_monitor_needs_install_proof(log):
    terminal = []
    store = operations.OperationStore(log, on_terminal=terminal.append)
    operation, _ = _stardew(store)
    steam = FakeSteam()
    installed = set()
    monitor = operations_monitors.SteamMonitor(
        store, steam, log, installed_probe=lambda: installed
    )
    steam.set(
        downloads=[
            {
                "appid": 413150,
                "name": "Stardew Valley",
                "percent": 25,
                "paused": False,
                "queue": 0,
            }
        ]
    )
    assert monitor.reconcile_once() == 1
    observed = store.get(operation["id"])
    assert observed["state"] == operations.RUNNING
    assert observed["progress"]["percent"] == 25

    steam.set(online=False)
    monitor.reconcile_once()
    assert store.get(operation["id"])["state"] == operations.UNKNOWN
    assert not terminal and not store.pending_announcements()

    steam.set(online=True, downloads=[])
    monitor.reconcile_once()
    assert store.get(operation["id"])["state"] == operations.UNKNOWN
    assert not terminal, "absence without install proof became terminal"

    steam.set(
        downloads=[
            {
                "appid": 413150,
                "name": "Stardew Valley",
                "percent": 100,
                "paused": False,
                "queue": 0,
            }
        ]
    )
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
    assert (
        store.observe(operation["id"], operations.UNKNOWN)["state"]
        == operations.SUCCEEDED
    )
    store.for_assistant("recent", acknowledge=True)
    heard = store.get(operation["id"])
    assert not heard["announcement_pending"] and heard["delivered"]


def test_cancel_is_refused_for_steam_installs(log):
    store = operations.OperationStore(log)
    other = store.track_steam_install(570, "Dota 2", verified=True)
    ok, detail = store.cancel(other["id"])
    assert not ok and "not supported" in detail
    assert store.get(other["id"])["state"] == operations.RUNNING
    assert store.for_assistant("active")[0]["state"] in operations.ACTIVE


def test_media_monitor_observes_a_series_acquisition(log):
    # The second authority justifies generic creation, but keeps its concrete
    # observation semantics in MediaMonitor.
    terminal = []
    store = operations.OperationStore(log, on_terminal=terminal.append)
    media_op = store.track_external(
        "series_acquisition",
        "sonarr",
        "41",
        "Breaking Bad",
        turn="beef01",
        detail="Sonarr accepted the request",
        metadata={
            "catalog_id": 81189,
            "preset": "1080p",
            "profile": "Series HD",
            "seasons": [2],
        },
    )
    reused = store.track_external(
        "series_acquisition",
        "sonarr",
        "41",
        "Breaking Bad",
        detail="Sonarr accepted the request",
        metadata={
            "catalog_id": 81189,
            "preset": "2160p",
            "profile": "Series UHD",
            "seasons": [2],
            "search_pending": True,
        },
    )
    assert reused["id"] == media_op["id"]
    assert reused["metadata"]["preset"] == "2160p"
    fake_media = FakeMedia()
    monitor = operations_monitors.MediaMonitor(store, fake_media, log)
    assert monitor.reconcile_once() == 1
    assert store.get(media_op["id"])["progress"]["percent"] == 20
    assert store.get(media_op["id"])["metadata"]["search_pending"]
    fake_media.set(search_ready=True)
    monitor.reconcile_once()
    assert "search_pending" not in store.get(media_op["id"])["metadata"]
    assert store.get(media_op["id"])["metadata"]["command_ids"] == [77]
    fake_media.set(error=RuntimeError("offline"))
    monitor.reconcile_once()
    assert store.get(media_op["id"])["state"] == operations.UNKNOWN
    assert not terminal
    fake_media.set(
        error=None,
        result={
            "complete": True,
            "progress": {"percent": 100},
            "detail": "5 of 5 aired episodes are ready",
        },
    )
    monitor.reconcile_once()
    media_done = store.get(media_op["id"])
    assert media_done["state"] == operations.SUCCEEDED
    assert media_done["summary"] == (
        "The requested episodes of Breaking Bad are ready to watch."
    )
    assert len(terminal) == 1
    monitor.reconcile_once()
    assert len(terminal) == 1
    ok, detail = store.cancel(media_op["id"])
    assert not ok and "already succeeded" in detail


def test_media_monitor_notifies_once_per_phase(log):
    store = operations.OperationStore(log)
    fake_media = FakeMedia()
    monitor = operations_monitors.MediaMonitor(store, fake_media, log)
    phase_op = _movie(store, "51", "Arrival", 329865, 9)
    fake_media.set(
        result={
            "complete": False,
            "progress": {"phase": "searching"},
            "detail": "Radarr is searching",
        }
    )
    monitor.reconcile_once()
    fake_media.set(
        result={
            "complete": False,
            "progress": {"phase": "waiting_for_match"},
            "detail": "no acceptable release yet",
        }
    )
    monitor.reconcile_once()
    assert len(store.pending_notifications()) == 1
    assert store.pending_notifications()[0]["key"] == "waiting_for_match"
    assert "search_retry_pending" not in store.get(phase_op["id"])["metadata"]
    fake_media.set(
        result={
            "complete": False,
            "progress": {"phase": "downloading", "percent": 2},
            "detail": "download is 2% complete",
        }
    )
    monitor.reconcile_once()
    assert {row["key"] for row in store.pending_notifications()} == {
        "waiting_for_match",
        "download_started",
    }
    monitor.reconcile_once()
    assert len(store.pending_notifications()) == 2


def test_search_retry_backs_off_then_gives_up(log):
    store = operations.OperationStore(log)
    retry_op = _movie(store, "61", "Heat", 949, 10)
    store.observe(
        retry_op["id"],
        operations.RUNNING,
        {"phase": "searching"},
        "Radarr is searching",
    )
    retry_media = FakeMedia(
        search_available_now=False,
        result={
            "complete": False,
            "progress": {"phase": "waiting_for_match"},
            "detail": "no acceptable release yet",
        },
    )
    retry_monitor = operations_monitors.MediaMonitor(store, retry_media, log)
    retry_monitor.reconcile_once(now=1000)
    scheduled = store.get(retry_op["id"])
    assert scheduled["metadata"]["search_retry_pending"]
    assert scheduled["metadata"]["search_retry_after"] == 1300
    assert not retry_media.retries

    retry_media.set(search_available_now=True)
    operations_monitors.MediaMonitor(store, retry_media, log).reconcile_once(now=1299)
    assert not retry_media.retries
    operations_monitors.MediaMonitor(store, retry_media, log).reconcile_once(now=1300)
    retried = store.get(retry_op["id"])
    assert retry_media.retries == [retry_op["id"]]
    assert retried["metadata"]["search_retry_count"] == 1
    assert retried["metadata"]["command_ids"] == [88]
    assert "search_retry_pending" not in retried["metadata"]

    store.update_metadata(
        retry_op["id"],
        {"search_retry_count": 3},
        remove=("search_retry_pending", "search_retry_after"),
    )
    store.observe(
        retry_op["id"],
        operations.RUNNING,
        {"phase": "searching"},
        "Radarr is searching",
    )
    retry_media.set(search_available_now=False)
    retry_monitor.reconcile_once(now=2000)
    exhausted = store.get(retry_op["id"])["metadata"]
    assert exhausted["search_retry_exhausted"]
    assert "search_retry_pending" not in exhausted


def test_failed_search_retry_backs_off_longer(log):
    store = operations.OperationStore(log)
    failed_op = _movie(
        store,
        "62",
        "Collateral",
        1538,
        11,
        search_retry_pending=True,
        search_retry_after=2000,
    )
    store.observe(
        failed_op["id"], operations.RUNNING, {"phase": "waiting_for_match"}, "waiting"
    )
    retry_media = FakeMedia(
        result={
            "complete": False,
            "progress": {"phase": "waiting_for_match"},
            "detail": "no acceptable release yet",
        },
        retry_error=RuntimeError("search submit failed"),
    )
    operations_monitors.MediaMonitor(store, retry_media, log).reconcile_once(now=2000)
    failed = store.get(failed_op["id"])
    assert failed["state"] == operations.UNKNOWN
    assert failed["metadata"]["search_retry_count"] == 1
    assert failed["metadata"]["search_retry_after"] == 3800
    assert failed["metadata"]["search_retry_pending"]


def test_cli_lists_operations(log):
    store = operations.OperationStore(log)
    retry_op = _movie(store, "61", "Heat", 949, 10)
    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        assert operations.main(["list"]) == 0
    assert retry_op["id"] in stdout.getvalue()


def test_waiting_series_is_abandoned_after_a_day(log):
    store = operations.OperationStore(log)
    give_media = FakeMedia(result=dict(WAITING))
    give_op = store.track_external(
        "series_acquisition",
        "sonarr",
        "71",
        "Rick and Morty",
        metadata={"catalog_id": 275274, "seasons": None},
    )
    give_monitor = operations_monitors.MediaMonitor(store, give_media, log)
    give_monitor.reconcile_once(now=5000)
    assert store.get(give_op["id"])["metadata"]["waiting_since"] == 5000
    assert not give_media.abandoned
    # The heads-up speaks while the user still has context: what is missing
    # and that the close is coming.
    heads_up = [
        item
        for item in store.pending_notifications()
        if item["operation_id"] == give_op["id"]
    ]
    assert len(heads_up) == 1 and heads_up[0]["key"] == "waiting_for_match"
    assert "11" in heads_up[0]["summary"]
    give_media.set(
        result={
            "complete": False,
            "progress": {"phase": "downloading", "total_episodes": 91},
            "detail": "download is active",
        }
    )
    give_monitor.reconcile_once(now=6000)  # a grab resets the clock
    assert "waiting_since" not in store.get(give_op["id"])["metadata"]
    give_media.set(result=dict(WAITING))
    give_monitor.reconcile_once(now=7000)
    give_monitor.reconcile_once(now=7000 + 24 * 3600 - 1)
    assert not give_media.abandoned
    assert store.get(give_op["id"])["state"] == operations.RUNNING
    give_monitor.reconcile_once(now=7000 + 24 * 3600)
    closed = store.get(give_op["id"])
    assert give_media.abandoned == [give_op["id"]]
    assert closed["state"] == operations.SUCCEEDED
    # The close is silent - the heads-up already said it was coming.
    assert not closed["announcement_pending"]
    assert "season 1" in closed["summary"]


def test_unaired_season_waits_indefinitely(log):
    store = operations.OperationStore(log)
    give_media = FakeMedia(
        result={
            "complete": False,
            "progress": {
                "phase": "waiting_for_match",
                "episodes": 0,
                "total_episodes": 0,
                "percent": 0,
            },
            "detail": "no requested monitored episodes have aired yet",
        }
    )
    unaired = store.track_external(
        "series_acquisition",
        "sonarr",
        "72",
        "Andor",
        metadata={"catalog_id": 393189, "seasons": [2]},
    )
    give_monitor = operations_monitors.MediaMonitor(store, give_media, log)
    give_monitor.reconcile_once(now=8000)
    give_monitor.reconcile_once(now=8000 + 48 * 3600)
    fresh = store.get(unaired["id"])
    assert fresh["state"] == operations.RUNNING
    assert "waiting_since" not in fresh["metadata"]
    assert not give_media.abandoned
    # Pre-air waiting is not "couldn't find": no heads-up either.
    assert not [
        item
        for item in store.pending_notifications()
        if item["operation_id"] == unaired["id"]
    ]


def test_empty_movie_wait_fails_silently(log):
    store = operations.OperationStore(log)
    give_media = FakeMedia(
        result={
            "complete": False,
            "progress": {"phase": "waiting_for_match", "percent": 0},
            "detail": "no acceptable movie release is available yet",
        },
        abandon_result={"have": 0, "missing": []},
    )
    empty = store.track_external(
        "movie_acquisition",
        "radarr",
        "73",
        "Obscure Film",
        metadata={"catalog_id": 999},
    )
    give_monitor = operations_monitors.MediaMonitor(store, give_media, log)
    give_monitor.reconcile_once(now=9000)
    store.update_metadata(
        empty["id"],
        {"search_retry_pending": True, "search_retry_after": 9000 + 200 * 3600},
    )
    give_monitor.reconcile_once(now=9000 + 48 * 3600)  # retry gate holds
    assert store.get(empty["id"])["state"] == operations.RUNNING
    store.update_metadata(
        empty["id"], remove=("search_retry_pending", "search_retry_after")
    )
    give_monitor.reconcile_once(now=9000 + 96 * 3600)
    failed_empty = store.get(empty["id"])
    assert failed_empty["state"] == operations.FAILED
    assert not failed_empty["announcement_pending"]
    assert failed_empty["summary"].startswith("No acceptable release")


def test_unmonitored_request_is_canceled(log):
    store = operations.OperationStore(log)
    canceled_op = store.track_external(
        "series_acquisition",
        "sonarr",
        "71",
        "Andor",
        metadata={"catalog_id": 393189, "seasons": [1]},
    )
    canceled_media = FakeMedia(
        result={
            "complete": False,
            "canceled": True,
            "progress": {"episodes": 0, "total_episodes": 0, "percent": 0},
            "detail": "requested episodes are unmonitored",
        }
    )
    operations_monitors.MediaMonitor(store, canceled_media, log).reconcile_once()
    assert store.get(canceled_op["id"])["state"] == operations.CANCELED


def test_delivery_retries_an_announcement_cut_short(log, monkeypatch):
    voice = dict(helpers.CONFIG["voice"])
    ann = announce.Announcer(voice, {"deepgramApiKey": "x" * 40}, log)
    store = operations.OperationStore(
        log, on_terminal=ann.submit, on_notification=ann.submit_notification
    )
    monkeypatch.setattr(ann, "store", store)
    monkeypatch.setattr(announce, "synth", lambda *a, **kw: b"speech")
    monkeypatch.setattr(ann, "_play", lambda pcm: False)
    pending = store.track_steam_install(20, "Team Fortress Classic", verified=True)
    store.observe(
        pending["id"], operations.SUCCEEDED, {"percent": 100}, "fully installed"
    )
    assert wait_for(lambda: bool(log.find("announce_cut_short")))
    assert store.get(pending["id"])["announcement_pending"]
    assert not ann.follow_up.is_set()

    monkeypatch.setattr(ann, "_play", lambda pcm: True)
    ann.submit(store.get(pending["id"]))
    assert wait_for(lambda: not store.get(pending["id"])["announcement_pending"])
    assert ann.follow_up.is_set()
    assert len(log.find("operation_announced")) == 1
    store.notify(
        pending["id"],
        "download_started",
        "Team Fortress Classic has started downloading.",
    )
    assert wait_for(
        lambda: (
            not any(
                row["operation_id"] == pending["id"]
                for row in store.pending_notifications()
            )
        )
    )
    ann.stop()
