"""Blind test: library.sync orchestration - layer 1 always, deals (layer 4)
keyless and staleness-gated, owned gated on staleness + key, metadata top-up
only, and the non-reentrant guard so rapid session-boundary calls can't stack
concurrent crawls. All layer fns are mocked; no network. Run:
    .venv\\Scripts\\python tests\\test_sync.py
"""
import _bootstrap  # noqa: F401
import threading
import time

import cglib
import library
import cglib
import library
import steamstore


def reset(monkey):
    for k in ("installed", "owned", "meta", "deals", "collections"):
        monkey[k] = 0
    library._sync_lock = threading.Lock()


def main():
    calls = {"installed": 0, "owned": 0, "meta": 0, "deals": 0, "collections": 0}
    state = {"index": {}, "refresh_rc": 0}

    # refresh returns 0 on success (PC awake); sync gates collections on that,
    # so the mock returns state["refresh_rc"] and a test can flip it to 1 to
    # simulate a sleeping PC. Everything is mocked - no ssh, no store.
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
    state["deals"] = {"refreshed": fresh}          # restore fresh for the rest

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
    # Both need the PC awake, so a failed installed refresh must not spend a
    # second ssh timeout on collections.
    reset(calls)
    state["refresh_rc"] = 1                         # refresh reports failure
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

    print("OK - sync: key-gate, deals (keyless+staleness), collections "
          "(gated on the PC being awake), owned staleness (6h), meta top-up, "
          "non-reentrant guard")


if __name__ == "__main__":
    main()
