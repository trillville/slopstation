"""Library.sync orchestration - layer 1 always, deals (layer 4)
keyless and staleness-gated, owned gated on staleness + key, metadata top-up
only, and the non-reentrant guard against stacked crawls. All layer fns are
mocked; no network.
"""

import threading
import time
import types

import pytest

from helpers import CapturingLog
from slopstation import config
from slopstation.agent.tools import library, steamstore

KEYLESS = {"steamApiKey": "dg_...", "steamId64": ""}
KEYED = {"steamApiKey": "X" * 40, "steamId64": "7656119"}

NOTHING = {"installed": 0, "owned": 0, "meta": 0, "deals": 0, "collections": 0}


def stamp(age_s=0):
    """An index timestamp age_s seconds old, in the form _iso_age parses."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - age_s))


@pytest.fixture
def layers(monkeypatch):
    """Every layer fn mocked to count into `calls`; `state` is what the loads
    answer (index, meta_cache, deals) and what layer 1 returns (refresh_rc).
    No Steam key, and deals fresh, so nothing beyond layers 1+1b runs until a
    test turns a knob."""
    calls = dict(NOTHING)
    state = {
        "index": {},
        "meta_cache": {},
        "deals": {"refreshed": stamp()},  # fresh by default -> deals skipped
        "refresh_rc": 0,
    }

    def hit(layer):
        calls[layer] += 1

    # refresh returns 0 on success (PC awake); sync gates collections on it.
    def mock_refresh(**k):
        hit("installed")
        return state["refresh_rc"]

    monkeypatch.setattr(library, "refresh", mock_refresh)
    monkeypatch.setattr(library, "refresh_collections", lambda: hit("collections"))
    monkeypatch.setattr(library, "refresh_owned", lambda: hit("owned"))
    monkeypatch.setattr(library, "refresh_meta", lambda appids, limit=200: hit("meta"))
    # Layer 4: deals is keyless and MUST be mocked here or sync() hits the store.
    monkeypatch.setattr(steamstore, "refresh_deals", lambda: hit("deals"))
    monkeypatch.setattr(steamstore, "load_deals", lambda: state["deals"])
    monkeypatch.setattr(library, "load", lambda: state["index"])
    monkeypatch.setattr(library, "load_meta", lambda: state["meta_cache"])
    # A lock of this test's own, so a stacked-crawl test cannot inherit one.
    monkeypatch.setattr(library, "_sync_lock", threading.Lock())
    monkeypatch.setattr(config, "secrets", lambda: dict(KEYLESS))
    return types.SimpleNamespace(calls=calls, state=state)


@pytest.fixture
def keyed(layers, monkeypatch):
    """The same layers with a Steam key and id on file."""
    monkeypatch.setattr(config, "secrets", lambda: dict(KEYED))
    return layers


def test_no_key_runs_layers_1_and_4_only(layers):
    # deals fresh here -> skipped
    layers.state["index"] = {"installed": [{"appid": 1}]}
    library.sync()
    assert layers.calls == {**NOTHING, "installed": 1, "collections": 1}, layers.calls


def test_deals_stale_refreshes_without_a_key(layers):
    # GetWishlist/specials are keyless.
    layers.state["deals"] = {}  # no stamp -> stale
    layers.state["index"] = {"installed": [{"appid": 1}]}
    library.sync()
    assert layers.calls == {
        **NOTHING,
        "installed": 1,
        "deals": 1,
        "collections": 1,
    }, layers.calls


def test_key_and_stale_owned_run_all_three(keyed):
    keyed.state["index"] = {
        "installed": [{"appid": 1}],
        "owned": {"2": {}},
        "ownedRefreshed": stamp(7 * 3600),  # >6h -> owned runs
    }
    keyed.state["meta_cache"] = {}  # nothing cached -> meta runs
    library.sync()
    assert keyed.calls == {
        **NOTHING,
        "installed": 1,
        "owned": 1,
        "meta": 1,
        "collections": 1,
    }, keyed.calls


def test_owned_fresh_and_meta_cached_skip_both(keyed):
    keyed.state["index"] = {
        "installed": [{"appid": 1}],
        "owned": {"2": {}},
        "ownedRefreshed": stamp(),  # <6h -> skip owned
    }
    keyed.state["meta_cache"] = {"1": {}, "2": {}}  # both known appids cached
    library.sync()
    assert keyed.calls == {**NOTHING, "installed": 1, "collections": 1}, keyed.calls


def test_pc_asleep_skips_collections(keyed):
    # The gate: both need the PC awake; don't spend a second ssh timeout here.
    keyed.state["refresh_rc"] = 1
    keyed.state["index"] = {"installed": []}
    library.sync()
    assert keyed.calls["installed"] == 1 and keyed.calls["collections"] == 0, (
        keyed.calls
    )


def test_second_sync_while_one_runs_is_a_noop(keyed, monkeypatch):
    keyed.state["index"] = {"installed": [{"appid": 1}]}
    keyed.state["meta_cache"] = {"1": {}}
    barrier = threading.Event()

    def slow_refresh(**k):
        keyed.calls["installed"] += 1
        barrier.wait(2)  # hold the lock

    monkeypatch.setattr(library, "refresh", slow_refresh)
    t = threading.Thread(target=library.sync)
    t.start()
    try:
        time.sleep(0.2)  # let it acquire the lock
        library.sync()  # must no-op immediately
        assert keyed.calls["installed"] == 1, "second sync should not have entered"
    finally:
        # Let the first sync finish while the layers are still mocked.
        barrier.set()
        t.join()


def test_refresh_reports_sync_done_and_sync_skipped(monkeypatch):
    """The events: the real layer 1 says sync_done / sync_skipped on the
    library lane."""
    log = CapturingLog("library")
    monkeypatch.setattr(library, "log", log)
    monkeypatch.setattr(
        library,
        "fetch_installed_ssh",
        lambda: [{"appid": 1, "name": "G", "state": 4, "size": 1, "lastPlayed": 0}],
    )
    assert library.refresh() == 0
    assert log.find("sync_done")[0]["layer"] == "installed"

    def asleep():
        raise OSError("ssh: connect timed out")

    monkeypatch.setattr(library, "fetch_installed_ssh", asleep)
    assert library.refresh() == 1
    skipped = log.find("sync_skipped")[0]
    assert skipped["layer"] == "installed" and skipped["level"] == "warn"
