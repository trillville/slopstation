"""Refresh_meta saves incrementally so a killed crawl (daemon
thread dies on Ctrl+C) keeps progress instead of re-crawling from zero.

The metadata cache lives under paths.HOME, which conftest points at this
test's tmp_path, so every test starts from an empty cache.
"""

import time

import pytest

from slopstation.agent.tools import library


@pytest.fixture
def fetched(monkeypatch):
    """fetch_meta_one answers a canned tag per appid and the request pacing
    sleep is removed. Returns the appids fetched, in order."""
    calls = []
    monkeypatch.setattr(
        library,
        "fetch_meta_one",
        lambda appid: calls.append(appid) or {"tags": [f"t{appid}"]},
    )
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return calls


def test_refresh_meta_saves_after_each_fetch(monkeypatch, fetched):
    # The cache is saved after EACH fetch, so a kill after item N leaves N on
    # disk, not zero.
    orig_save = library._save_meta
    sizes = []

    def counting_save(cache):
        orig_save(cache)
        sizes.append(len(cache))

    monkeypatch.setattr(library, "_save_meta", counting_save)
    library.refresh_meta([1, 2, 3])
    assert sizes == [1, 2, 3], (
        f"saved at {sizes} - not incremental (batched saves once)"
    )
    assert len(library.load_meta()) == 3


def test_refresh_meta_resumes_fetching_only_the_missing(fetched):
    # Resume after a kill: three appids already on disk, so only the missing
    # ones are fetched.
    library._save_meta({str(a): {"tags": [f"t{a}"]} for a in (1, 2, 3)})
    library.refresh_meta([1, 2, 3, 4, 5])
    assert fetched == [4, 5], f"re-fetched {fetched}, should only do the missing 2"
    assert len(library.load_meta()) == 5


def test_query_terms_ranks_the_vocabulary_by_frequency():
    # query_terms: the ask-about-games vocabulary, frequency-ranked.
    library._save_meta(
        {
            "1": {"tags": ["Mechs", "Action"], "genres": []},
            "2": {"tags": ["Mechs"], "genres": ["RPG"]},
            "3": {"tags": ["Roguelike", "Mechs"], "genres": []},
        }
    )
    terms = library.query_terms()
    assert terms[0] == "mechs", terms  # 3 games carry it
    assert {"action", "rpg", "roguelike"} <= set(terms), terms
    assert library.query_terms(limit=1) == ["mechs"]
