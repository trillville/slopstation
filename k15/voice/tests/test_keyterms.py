"""Blind test: the vocabulary handed to Flux - offline, no Deepgram, no audio.
Pins the list's shape; bench/probe_stt.py is the live counterpart. Run:
    .venv\\Scripts\\python tests\\test_keyterms.py
"""
import sys
from pathlib import Path

import _bootstrap                               # noqa: F401,E402

import library                                  # noqa: E402
import session_runtime                          # noqa: E402
import titles                                   # noqa: E402
from grammar_gate import stt_confidence         # noqa: E402

VOICE = {"keytermCount": 40}

FAKE_INDEX = {
    "installed": [
        {"appid": 1888160, "name": "ARMORED CORE VI FIRES OF RUBICON",
         "lastPlayed": 900},
        {"appid": 1145360, "name": "Hades II", "lastPlayed": 800},
        {"appid": 1361210, "name": "Warhammer 40,000: Darktide", "lastPlayed": 700},
        {"appid": 228980, "name": "Steamworks Common Redistributables",
         "lastPlayed": 999},                    # library.NOT_GAMES
    ],
    "collections": [{"name": "mech", "id": "uc-1"}, {"name": "RPG", "id": "uc-2"}],
}


class Frame:
    def __init__(self, result):
        self.result = result


def test_forms_teach_the_short_name_a_person_says():
    forms = titles.keyterm_forms("ARMORED CORE VI FIRES OF RUBICON")
    assert "armored core 6" in forms, forms
    assert "armored core 6 fires of rubicon" in forms, forms
    assert not any(f != f.lower() for f in forms), forms
    print("  OK  a mid-title digit ends the name -> 'armored core 6'")


def test_forms_do_not_cut_a_multi_token_number():
    forms = titles.keyterm_forms("Warhammer 40,000: Darktide")
    assert "warhammer 40 000" in forms, forms
    assert "warhammer 40" not in forms, "cut 40,000 in half"
    print("  OK  40,000 survives; 'warhammer 40' is never taught")


def test_trailing_sequel_number_yields_the_bare_name():
    # Only Hades II is installed, so this is how plain 'Hades' is covered.
    assert titles.keyterm_forms("Hades II") == ["hades 2", "hades"]
    print("  OK  'Hades II' also teaches 'hades'")


def test_vocabulary_covers_all_three_sources_and_drops_non_games():
    terms = session_runtime.stt_keyterms(VOICE, "hey jarvis")
    assert terms[0] == "hey jarvis", terms[:1]
    assert "armored core 6" in terms, "titles missing"
    assert "mech" in terms, "collection names missing - this is the mech/neck bug"
    assert "rpg" in terms, "collection names must be normalised like everything else"
    assert not any("redistributables" in t for t in terms), "NOT_GAMES leaked in"
    assert len(terms) == len(set(terms)), "duplicate keyterms spend boost twice"
    print(f"  OK  {len(terms)} terms: titles + collections + query words, deduped")


def test_the_cap_is_announced_not_silent():
    real = session_runtime.MAX_KEYTERMS
    session_runtime.MAX_KEYTERMS = 3
    try:
        import cglib
        log = cglib.CapturingLog()
        session_runtime.log, saved = log, session_runtime.log
        try:
            terms = session_runtime.stt_keyterms(VOICE, "hey jarvis")
        finally:
            session_runtime.log = saved
    finally:
        session_runtime.MAX_KEYTERMS = real
    assert len(terms) == 3, terms
    capped = log.find("keyterms_capped")
    assert capped and capped[0]["dropped"] > 0, log.events()
    print("  OK  truncation logs what it dropped")


def test_the_cap_never_exceeds_deepgram_s_measured_ceiling():
    """100 keyterms connect, 110 are refused with HTTP 400 at the handshake
    (measured 2026-08-15). Over the cap, every session fails to open."""
    assert session_runtime.MAX_KEYTERMS <= 100, session_runtime.MAX_KEYTERMS
    print("  OK  cap stays at or under the measured hard limit of 100")


def test_confidence_is_read_and_never_raises():
    assert stt_confidence(Frame({"words": [{"confidence": 0.9},
                                           {"confidence": 0.7}]})) == 0.8
    for bad in ({}, {"words": []}, {"words": [{}]}, {"words": "nope"}, None):
        assert stt_confidence(Frame(bad)) is None, bad
    class Exploding:
        @property
        def result(self):
            raise RuntimeError("upstream moved")
    assert stt_confidence(Exploding()) is None, "telemetry cost the turn"
    print("  OK  confidence averaged; malformed payloads cost the field, not the turn")


def main():
    real_load, real_terms = library.load, library.query_terms
    library.load = lambda *a, **k: FAKE_INDEX
    library.query_terms = lambda *a, **k: ["action", "roguelike"]
    try:
        test_forms_teach_the_short_name_a_person_says()
        test_forms_do_not_cut_a_multi_token_number()
        test_trailing_sequel_number_yields_the_bare_name()
        test_vocabulary_covers_all_three_sources_and_drops_non_games()
        test_the_cap_is_announced_not_silent()
        test_the_cap_never_exceeds_deepgram_s_measured_ceiling()
        test_confidence_is_read_and_never_raises()
    finally:
        library.load, library.query_terms = real_load, real_terms
    print("OK - stt vocabulary: spoken forms, three sources, loud cap, safe confidence")


if __name__ == "__main__":
    main()
