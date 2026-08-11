"""Blind test: library.sync orchestration - layer 1 always, owned
gated on staleness + key, metadata top-up only, and the non-reentrant guard so
rapid session-boundary calls can't stack concurrent crawls. All layer fns are
mocked; no network. Run:
    .venv\\Scripts\\python tests\\test_sync.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cglib
import library


def reset(monkey):
    monkey["installed"] = monkey["owned"] = monkey["meta"] = 0
    library._sync_lock = threading.Lock()


def main():
    calls = {"installed": 0, "owned": 0, "meta": 0}
    state = {"index": {}}

    library.refresh = lambda **k: calls.__setitem__("installed", calls["installed"] + 1)
    library.refresh_owned = lambda: calls.__setitem__("owned", calls["owned"] + 1)
    library.refresh_meta = lambda appids, limit=200: calls.__setitem__("meta", calls["meta"] + 1)
    library.load = lambda: state["index"]
    library.load_meta = lambda: state["meta_cache"]
    state["meta_cache"] = {}

    # --- no Steam key: layer 1 only ------------------------------------------
    cglib.load_secrets = lambda: {"steamApiKey": "dg_...", "steamId64": ""}
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}]}
    library.sync()
    assert calls == {"installed": 1, "owned": 0, "meta": 0}, calls

    # --- key present, owned stale (no timestamp) -> all three -----------------
    cglib.load_secrets = lambda: {"steamApiKey": "X" * 40, "steamId64": "7656119"}
    reset(calls)
    state["index"] = {"installed": [{"appid": 1}], "owned": {"2": {}}}
    state["meta_cache"] = {}                       # nothing cached -> meta runs
    library.sync()
    assert calls == {"installed": 1, "owned": 1, "meta": 1}, calls

    # --- owned fresh (<6h) -> skip owned; all meta cached -> skip meta --------
    reset(calls)
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S")
    state["index"] = {"installed": [{"appid": 1}], "owned": {"2": {}},
                      "ownedRefreshed": fresh}
    state["meta_cache"] = {"1": {}, "2": {}}       # both known appids cached
    library.sync()
    assert calls == {"installed": 1, "owned": 0, "meta": 0}, calls

    # --- owned stale (>6h) -> refresh owned -----------------------------------
    reset(calls)
    old = time.strftime("%Y-%m-%dT%H:%M:%S",
                        time.localtime(time.time() - 7 * 3600))
    state["index"] = {"installed": [{"appid": 1}], "owned": {},
                      "ownedRefreshed": old}
    state["meta_cache"] = {"1": {}}
    library.sync()
    assert calls["owned"] == 1, calls

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

    print("OK - sync: key-gate, owned staleness (6h), meta top-up, "
          "non-reentrant guard")


if __name__ == "__main__":
    main()
