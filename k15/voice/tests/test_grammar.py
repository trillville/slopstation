"""Blind test (C1 s3): the Tier-1 grammar, offline - utterance -> intent+slots
table, negatives that MUST fall through to the assistant lane, and the
risky-command narrowness rule. Run:
    .venv\\Scripts\\python tests\\test_grammar.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grammar_gate import GrammarMatcher

VOICE_CFG = {"inputs": {"apple tv": "hdmi1", "playstation": "hdmi2",
                        "ps5": "hdmi2", "the pc": "hdmi4"}}
TITLES = ["armored core six", "elden ring", "forza horizon five"]

# (utterance, expected intent or None, expected slots subset)
TABLE = [
    ("start a session", "StartSession", {}),
    ("start the gaming session", "StartSession", {}),
    ("game time", "StartSession", {}),
    ("let's play", "StartSession", {}),
    ("end the session", "EndSession", {}),
    ("end session", "EndSession", {}),
    ("we're done", "EndSession", {}),
    ("we're done gaming", "EndSession", {}),
    ("volume up", "VolumeUp", {}),
    ("turn the volume up", "VolumeUp", {}),
    ("turn it up", "VolumeUp", {}),
    ("louder", "VolumeUp", {}),
    ("volume down", "VolumeDown", {}),
    ("quieter", "VolumeDown", {}),
    ("set the volume to 25", "VolumeSet", {"level": 25}),
    ("volume 30", "VolumeSet", {"level": 30}),
    ("set volume to 100", "VolumeSet", {"level": 100}),
    ("mute", "MuteToggle", {}),
    ("mute the tv", "MuteToggle", {}),
    ("unmute the sound", "MuteToggle", {}),
    ("switch to the apple tv", "SwitchInput", {"input": "apple tv"}),
    ("go back to the playstation", "SwitchInput", {"input": "playstation"}),
    ("switch to ps5", "SwitchInput", {"input": "ps5"}),
    ("show the apple tv", "SwitchInput", {"input": "apple tv"}),
    # {game} is a wildcard: the slot carries the (normalized) spoken text;
    # title->appid resolution is titles.py's job, tested in test_library.
    ("play armored core six", "PlayGame", {"game": "armored core 6"}),
    ("launch elden ring", "PlayGame", {"game": "elden ring"}),
    ("put on the game forza horizon five", "PlayGame", {"game": "forza horizon 5"}),
    ("start elden ring", "PlayGame", {"game": "elden ring"}),
    ("play some music", "PlayGame", {"game": "some music"}),
    ("thanks", "ExitSession", {}),
    ("that's all", "ExitSession", {}),
    ("never mind", "ExitSession", {}),
    # --- MUST fall through (assistant lane / no action) ----------------------
    ("what mech games do i have", None, {}),
    ("suggest a shooter i haven't played in a while", None, {}),
    ("hello there", None, {}),
    ("start", None, {}),
    ("play", None, {}),
    ("switch to the garage", None, {}),          # unknown input name
    # Risky-command narrowness: casual variants must NOT end a session.
    ("end it", None, {}),
    ("stop", None, {}),
    ("kill the session please maybe", None, {}),
]


def main():
    m = GrammarMatcher(VOICE_CFG, TITLES)
    failures = []
    for text, want_intent, want_slots in TABLE:
        got = m.match(text)
        if want_intent is None:
            if got is not None:
                failures.append(f"'{text}': expected NO match, got {got}")
            continue
        if got is None:
            failures.append(f"'{text}': expected {want_intent}, got no match")
            continue
        intent, slots = got
        if intent != want_intent:
            failures.append(f"'{text}': expected {want_intent}, got {intent}")
            continue
        for k, v in want_slots.items():
            got_v = slots.get(k)
            if isinstance(v, (int, float)):
                ok = got_v is not None and float(got_v) == float(v)
            else:
                ok = str(got_v).lower() == str(v).lower()
            if not ok:
                failures.append(f"'{text}': slot {k}={got_v!r}, want {v!r}")

    for f in failures:
        print("FAIL", f)
    assert not failures, f"{len(failures)} grammar failures"
    print(f"OK - {len(TABLE)} utterances: intents, slots, fall-throughs, "
          f"risky-command narrowness")


if __name__ == "__main__":
    main()
