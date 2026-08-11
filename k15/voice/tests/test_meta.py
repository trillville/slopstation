"""Blind test: refresh_meta saves incrementally so a killed crawl (daemon
thread dies on Ctrl+C) keeps progress instead of re-crawling from zero. Run:
    .venv\\Scripts\\python tests\\test_meta.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import library


def main():
    tmp = Path(tempfile.mkdtemp())
    library.STATE = tmp
    library.META_CACHE = tmp / "metadata-cache.json"

    fetched = []
    library.fetch_meta_one = lambda appid: fetched.append(appid) or {"tags": [f"t{appid}"]}
    real_sleep = time.sleep
    time.sleep = lambda s: None

    # Incremental invariant: the cache is saved after EACH fetch (sizes 1,2,3),
    # not once at the end (which would be a single save of size 3). A daemon-
    # thread kill after item N therefore leaves N on disk, not zero.
    orig_save = library._save_meta
    sizes = []

    def counting_save(cache):
        orig_save(cache)
        sizes.append(len(cache))

    library._save_meta = counting_save
    library.refresh_meta([1, 2, 3])
    assert sizes == [1, 2, 3], f"saved at {sizes} - not incremental (batched saves once)"
    assert len(library.load_meta()) == 3

    # Resume after a "kill": only the missing appids are fetched (top-up).
    library._save_meta = orig_save
    fetched.clear()
    library.refresh_meta([1, 2, 3, 4, 5])
    assert fetched == [4, 5], f"re-fetched {fetched}, should only do the missing 2"
    assert len(library.load_meta()) == 5

    time.sleep = real_sleep
    print("OK - refresh_meta: saves after each fetch, resume does top-up only")


if __name__ == "__main__":
    main()
