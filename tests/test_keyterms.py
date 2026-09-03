"""The vocabulary handed to Flux - offline, no Deepgram, no audio.
Pins the list's shape; bench/probe_stt.py is the live counterpart.
"""

import dataclasses

import pytest

from slopstation import logbook
from slopstation.agent.speech import session_runtime
from slopstation.agent.speech.grammar_gate import stt_confidence
from slopstation.agent.tools import library, titles

VOICE = {"keytermCount": 40}

FAKE_INDEX = {
    "installed": [
        {
            "appid": 1888160,
            "name": "ARMORED CORE VI FIRES OF RUBICON",
            "lastPlayed": 900,
        },
        {"appid": 1145360, "name": "Hades II", "lastPlayed": 800},
        {"appid": 1361210, "name": "Warhammer 40,000: Darktide", "lastPlayed": 700},
        {
            "appid": 228980,
            "name": "Steamworks Common Redistributables",
            "lastPlayed": 999,
        },  # library.NOT_GAMES
    ],
    "collections": [{"name": "mech", "id": "uc-1"}, {"name": "RPG", "id": "uc-2"}],
}


@pytest.fixture(autouse=True)
def _fake_catalog(monkeypatch):
    """The catalog the vocabulary is built from, and the tag words as SteamSpy
    writes them: title case, hyphens, generic head."""
    monkeypatch.setattr(library, "load", lambda *a, **k: FAKE_INDEX)
    monkeypatch.setattr(
        library, "query_terms", lambda *a, **k: ["Action", "Rogue-like", "Mechs"]
    )


@dataclasses.dataclass
class Frame:
    result: object


def test_forms_teach_the_short_name_a_person_says():
    forms = titles.keyterm_forms("ARMORED CORE VI FIRES OF RUBICON")
    assert "armored core 6" in forms, forms
    assert "armored core 6 fires of rubicon" in forms, forms
    assert not any(f != f.lower() for f in forms), forms


def test_forms_do_not_cut_a_multi_token_number():
    forms = titles.keyterm_forms("Warhammer 40,000: Darktide")
    assert "warhammer 40 000" in forms, forms
    assert "warhammer 40" not in forms, "cut 40,000 in half"


def test_trailing_sequel_number_yields_the_bare_name():
    # Only Hades II is installed, so this is how plain 'Hades' is covered.
    assert titles.keyterm_forms("Hades II") == ["hades 2", "hades"]


def test_vocabulary_covers_all_three_sources_and_drops_non_games():
    terms = session_runtime.stt_keyterms(VOICE, "hey jarvis")
    assert terms[0] == "hey jarvis", terms[:1]
    assert "armored core 6" in terms, "titles missing"
    assert "mech" in terms, "collection names missing - this is the mech/neck bug"
    assert "rpg" in terms, "collection names must be normalised like everything else"
    assert not any("redistributables" in t for t in terms), "NOT_GAMES leaked in"
    assert len(terms) == len(set(terms)), "duplicate keyterms spend boost twice"


def test_query_words_are_spoken_forms_not_steamspy_strings():
    """SteamSpy writes 'Rogue-like'; the couch says 'rogue like'."""
    terms = session_runtime.query_keyterms()
    assert "rogue like" in terms, terms
    assert not any("-" in t or t != t.lower() for t in terms), terms


def test_generic_english_does_not_take_a_slot():
    """A keyterm buys a prior Flux lacks. It already has 'action'."""
    terms = session_runtime.query_keyterms()
    assert "action" not in terms, terms
    assert "mechs" in terms, "the mishear query_terms exists for was filtered out"


def test_the_cap_is_announced_not_silent(monkeypatch):
    log = logbook.CapturingLog()
    monkeypatch.setattr(session_runtime, "MAX_KEYTERMS", 3)
    monkeypatch.setattr(session_runtime, "log", log)
    terms = session_runtime.stt_keyterms(VOICE, "hey jarvis")
    assert len(terms) == 3, terms
    capped = log.find("keyterms_capped")
    assert capped and capped[0]["dropped"] > 0, log.events()


def test_the_cap_never_exceeds_deepgram_s_measured_ceiling():
    """100 keyterms connect, 110 are refused with HTTP 400 at the handshake
    (measured 2026-08-15). Over the cap, every session fails to open."""
    assert session_runtime.MAX_KEYTERMS <= 100, session_runtime.MAX_KEYTERMS


def test_confidence_is_read_and_never_raises():
    assert (
        stt_confidence(Frame({"words": [{"confidence": 0.9}, {"confidence": 0.7}]}))
        == 0.8
    )
    for bad in ({}, {"words": []}, {"words": [{}]}, {"words": "nope"}, None):
        assert stt_confidence(Frame(bad)) is None, bad

    class Exploding:
        @property
        def result(self):
            raise RuntimeError("upstream moved")

    assert stt_confidence(Exploding()) is None, "telemetry cost the turn"
