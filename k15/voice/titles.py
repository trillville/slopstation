"""Game-title normalization: the bridge between what Steam calls a game
(ARMORED CORE(tm) VI FIRES OF RUBICON) and what a human says on a couch
("play armored core six").

Two normal forms:
  spoken_form  - light: lowercase, trademark junk stripped, roman numerals and
                 number words unified to digits, apostrophes KEPT (transcripts
                 have them). Feeds hassil {game} slot variants - hassil matches
                 exact token sequences, so variants must look like transcripts.
  fuzzy_norm   - aggressive: spoken_form minus all punctuation. Feeds rapidfuzz.

Variant expansion per title: full form + subtitle-stripped form (text before
":" or " - ") + trailing-number-stripped form. Variants claimed by more than
one title are dropped as ambiguous (fuzzy resolution still sees full names).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cglib

LIBRARY = cglib.BASE / "state" / "library.json"

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
          "vii": "7", "viii": "8", "ix": "9", "xi": "11", "xii": "12"}
_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
          "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def spoken_form(s):
    s = s.lower()
    s = re.sub(r"[™®©]", "", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    tokens = [(_ROMAN.get(t) or _WORDS.get(t) or t) for t in s.split()]
    return " ".join(tokens)


def fuzzy_norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", spoken_form(s)).strip()


def variants(title):
    """Spoken-matchable forms of one raw title, most-specific first."""
    out = []
    full = spoken_form(title)
    if full:
        out.append(full)
    nosub = spoken_form(re.split(r"[:–—]| - ", title)[0])
    if nosub and nosub not in out:
        out.append(nosub)
    nonum = re.sub(r"\s+\d+$", "", nosub).strip()
    if nonum and nonum not in out:
        out.append(nonum)
    return out


def variant_map(titles):
    """variant -> canonical title, two-pass: every title's FULL spoken form
    always claims its own key (Portal must not lose 'portal' to Portal 2's
    number-stripped variant); derived variants (subtitle/number-stripped) only
    claim unclaimed keys, and derived-vs-derived collisions drop the key
    (ambiguous - fuzzy scoring handles those phrases instead)."""
    out = {}
    for t in titles:
        full = spoken_form(t)
        if full and full not in out:
            out[full] = t
    derived_claims = {}
    for t in titles:
        for v in variants(t)[1:]:
            if v not in out:
                derived_claims.setdefault(v, set()).add(t)
    for v, owners in derived_claims.items():
        if len(owners) == 1:
            out[v] = next(iter(owners))
    return out


def slot_tuples(titles):
    """[(variant, canonical_title)] for hassil's {game} slot."""
    return list(variant_map(titles).items())


def load_installed():
    try:
        return json.loads(LIBRARY.read_text(encoding="utf-8"))["installed"]
    except (OSError, KeyError, ValueError):
        return []


def build_resolver(threshold, margin=5):
    """spoken -> (appid, canonical title) or (None, None). Fuzzy over the
    culled variant space; a near-tie between DIFFERENT games (token_set_ratio
    scores subsets at 100, so 'warhammer' ties every 40K title) resolves to
    nothing rather than a coin flip - saying no beats launching wrong."""
    rows = load_installed()
    if not rows:
        return None
    by_name = {r["name"]: r["appid"] for r in rows if r.get("name")}
    vmap = {fuzzy_norm(v): canon
            for v, canon in variant_map(list(by_name)).items() if fuzzy_norm(v)}
    keys = list(vmap)
    from rapidfuzz import fuzz, process

    def resolve(spoken):
        q = fuzzy_norm(spoken)
        if q in vmap:                           # exact variant: no fuzz, no
            canon = vmap[q]                     # ambiguity ('hades 2' must
            return by_name[canon], canon        # never lose to 'hades')
        hits = process.extract(q, keys, scorer=fuzz.token_set_ratio, limit=3)
        if not hits or hits[0][1] < threshold:
            return None, None
        canon = vmap[hits[0][0]]
        for key, score, _ in hits[1:]:
            if vmap[key] != canon and score > hits[0][1] - margin:
                return None, None               # ambiguous across games
        return by_name[canon], canon

    return resolve
