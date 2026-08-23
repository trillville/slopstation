"""Blind test: refresh_meta saves incrementally so a killed crawl (daemon
thread dies on Ctrl+C) keeps progress instead of re-crawling from zero. Run:
    .venv\\Scripts\\python tests\\test_meta.py
"""
import sys
import time

import _bootstrap  # noqa: F401
from _bootstrap import fresh_state
import library


def main():
    fresh_state()

    fetched = []
    library.fetch_meta_one = lambda appid: fetched.append(appid) or {"tags": [f"t{appid}"]}
    real_sleep = time.sleep
    time.sleep = lambda s: None

    # The cache is saved after EACH fetch, so a kill after item N leaves N on
    # disk, not zero.
    orig_save = library._save_meta
    sizes = []

    def counting_save(cache):
        orig_save(cache)
        sizes.append(len(cache))

    library._save_meta = counting_save
    library.refresh_meta([1, 2, 3])
    assert sizes == [1, 2, 3], f"saved at {sizes} - not incremental (batched saves once)"
    assert len(library.load_meta()) == 3

    # Resume after a kill: only the missing appids are fetched.
    library._save_meta = orig_save
    fetched.clear()
    library.refresh_meta([1, 2, 3, 4, 5])
    assert fetched == [4, 5], f"re-fetched {fetched}, should only do the missing 2"
    assert len(library.load_meta()) == 5

    # query_terms: the ask-about-games vocabulary, frequency-ranked.
    library._save_meta({
        "1": {"tags": ["Mechs", "Action"], "genres": []},
        "2": {"tags": ["Mechs"], "genres": ["RPG"]},
        "3": {"tags": ["Roguelike", "Mechs"], "genres": []},
    })
    terms = library.query_terms()
    assert terms[0] == "mechs", terms                 # 3 games carry it
    assert {"action", "rpg", "roguelike"} <= set(terms), terms
    assert library.query_terms(limit=1) == ["mechs"]

    time.sleep = real_sleep
    print("OK - refresh_meta: saves after each fetch, resume does top-up only; "
          "query_terms ranks the tag/genre vocabulary")


if __name__ == "__main__":
    main()
