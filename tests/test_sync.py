"""Blind test: library.sync orchestration - layer 1 always, deals (layer 4)
keyless and staleness-gated, owned gated on staleness + key, metadata top-up
only, and the non-reentrant guard against stacked crawls. All layer fns are
mocked; no network. Run:
    pytest tests/test_sync.py
"""
import threading
import time

from slopstation import cglib
from slopstation.agent.tools import library
from slopstation.agent.tools import steamstore


def reset(monkey):
    for k in ("installed", "owned", "meta", "deals", "collections"):
        monkey[k] = 0
    library._sync_lock = threading.Lock()


def test_sync():
    real_refresh = library.refresh
    calls = {"installed": 0, "owned": 0, "meta": 0, "deals": 0, "collections": 0}
    state = {"index": {}, "refresh_rc": 0}

    # refresh returns 0 on success (PC awake); sync gates collections on it.
    def mock_refresh(**k):
        calls["installed"] += 1
        return state["refresh_rc"]
    library.refresh = mock_refresh
    library.refresh_collections = lambda: calls.__setitem__("collections", calls["collections"] + 1)
    library.refresh_owned = lambda: calls.__setitem__("owned", calls["owned"] + 1)
    library.refresh_meta = lambda appids, limit=200: calls.__setitem__("meta", calls["meta"] + 1)
    # Layer 4: deals is keyless and MUST be mocked here or sync() hits the store.
    steamstore.refresh_deals = lambda: calls.__setitem__("deals", calls["deals"] + 1)
    steamstore.load_deals = lambda: state["deals"]
    library.load = lambda: state["index"]
    library.load_meta = lambda: state["meta_cache"]
    state["meta_cache"] = {}
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S")
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7 * 3600))
    state["deals"] = {"refreshed": fresh}          # fresh by default -> deals skipped

    # --- no Steam key: layers 1+4 only (deals fresh here -> skipped) ----------
    cglib.load_secrets = lambda: {"steamApiKey": "dg_...", "steamId64": ""}
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}]}
    library.sync()
    assert calls == {"installed": 1, "owned": 0, "meta": 0, "deals": 0, "collections": 1}, calls

    # --- deals stale, NO key: still refreshes (GetWishlist/specials keyless) --
    reset(calls)
    state["deals"] = {}                            # no stamp -> stale
    state["index"] = {"installed": [{"appid": 1}]}
    library.sync()
    assert calls == {"installed": 1, "owned": 0, "meta": 0, "deals": 1, "collections": 1}, calls
    state["deals"] = {"refreshed": fresh}

    # --- key present, owned stale (no timestamp) -> all three -----------------
    cglib.load_secrets = lambda: {"steamApiKey": "X" * 40, "steamId64": "7656119"}
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}], "owned": {"2": {}}}
    state["meta_cache"] = {}                       # nothing cached -> meta runs
    library.sync()
    assert calls == {"installed": 1, "owned": 1, "meta": 1, "deals": 0, "collections": 1}, calls

    # --- owned fresh (<6h) -> skip owned; all meta cached -> skip meta --------
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}], "owned": {"2": {}},
                      "ownedRefreshed": fresh}
    state["meta_cache"] = {"1": {}, "2": {}}       # both known appids cached
    library.sync()
    assert calls == {"installed": 1, "owned": 0, "meta": 0, "deals": 0, "collections": 1}, calls

    # --- owned stale (>6h) -> refresh owned -----------------------------------
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}], "owned": {},
                      "ownedRefreshed": old}
    state["meta_cache"] = {"1": {}}
    library.sync()
    assert calls["owned"] == 1, calls

    # --- deals stale (>6h) -> refresh deals -----------------------------------
    reset(calls)
    state["deals"] = {"refreshed": old}
    state["index"] = {"installed": [{"appid": 1}]}
    library.sync()
    assert calls["deals"] == 1, calls
    state["deals"] = {"refreshed": fresh}

    # --- PC asleep (refresh != 0) -> collections is SKIPPED (the gate) --------
    # Both need the PC awake; don't spend a second ssh timeout here.
    reset(calls)
    state["refresh_rc"] = 1
    state["index"] = {"installed": []}
    library.sync()
    assert calls["installed"] == 1 and calls["collections"] == 0, calls
    state["refresh_rc"] = 0

    # --- non-reentrant: a second sync while one runs is a no-op --------------
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}]}
    state["meta_cache"] = {"1": {}}
    cglib.load_secrets = lambda: {"steamApiKey": "X" * 40, "steamId64": "7656119"}
    barrier = threading.Event()

    def slow_refresh(**k):
        calls["installed"] += 1
        barrier.wait(2)                            # hold the lock
    library.refresh = slow_refresh
    t = threading.Thread(target=library.sync)
    t.start()
    time.sleep(0.2)                                # let it acquire the lock
    library.sync()                                 # must no-op immediately
    assert calls["installed"] == 1, "second sync should not have entered"
    barrier.set()
    t.join()

    # --- the events: refresh says sync_done / sync_skipped on the library lane --
    from helpers import fresh_state
    fresh_state()
    library.log = cglib.CapturingLog("library")
    library.fetch_installed_ssh = lambda: [{"appid": 1, "name": "G", "state": 4,
                                            "size": 1, "lastPlayed": 0}]
    assert real_refresh() == 0
    assert library.log.find("sync_done")[0]["layer"] == "installed"

    def asleep():
        raise OSError("ssh: connect timed out")
    library.fetch_installed_ssh = asleep
    assert real_refresh() == 1
    skipped = library.log.find("sync_skipped")[0]
    assert skipped["layer"] == "installed" and skipped["level"] == "warn"
    print("  events: sync_done / sync_skipped (warn) from refresh, lane library")

    print("OK - sync: key-gate, deals (keyless+staleness), collections "
          "(gated on the PC being awake), owned staleness (6h), meta top-up, "
          "non-reentrant guard")
