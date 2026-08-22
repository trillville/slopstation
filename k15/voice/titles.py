"""Game-title normalization: Steam's title string <-> what a human says.

spoken_form feeds hassil {game} slot variants; hassil matches exact token
sequences, so variants must look like transcripts - hence apostrophes KEPT and
numerals unified to digits, which is what numerals=true makes Flux emit.
fuzzy_norm drops the rest of the punctuation and feeds rapidfuzz.
"""
import re

import library

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


def keyterm_forms(title):
    """STT keyterms for one title, most-specific first.

    Matched against what Flux EMITS, so spoken_form, not Steam's raw string
    (which cost 11 of 12 launches). Unlike variants(), must cover the short
    name a person says: a lone digit mid-title ends the name proper ('armored
    core 6 fires of rubicon' -> 'armored core 6'). One digit only, and the
    trailing-number strip is skipped when what remains still ends in a digit -
    else 'warhammer 40,000' becomes 'warhammer 40'."""
    out = []

    def add(v):
        if v and v not in out:
            out.append(v)

    full = spoken_form(title)
    add(full)
    nosub = spoken_form(re.split(r"[:–—]| - ", title)[0])
    add(nosub)
    toks = full.split()
    for i, t in enumerate(toks[:-1]):
        if len(t) == 1 and t.isdigit():
            add(" ".join(toks[:i + 1]))
            break
    nonum = re.sub(r"\s+\d+$", "", nosub).strip()
    if not re.search(r"\d$", nonum):
        add(nonum)
    return out


def variant_map(titles):
    """variant -> canonical title. A title's FULL spoken form always claims its
    own key (Portal must not lose 'portal' to Portal 2's number-stripped
    variant); derived variants claim only unclaimed keys, and
    derived-vs-derived collisions drop the key as ambiguous."""
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


def _resolver_from(by_name, threshold, margin=5):
    """Fuzzy resolver over any {name: id} map. A near-tie between DIFFERENT
    entries resolves to nothing: token_set_ratio scores subsets at 100, so
    'warhammer' ties every 40K title."""
    if not by_name:
        return None
    vmap = {fuzzy_norm(v): canon
            for v, canon in variant_map(list(by_name)).items() if fuzzy_norm(v)}
    keys = list(vmap)
    from rapidfuzz import fuzz, process

    def resolve(spoken):
        q = fuzzy_norm(spoken)
        if q in vmap:                           # exact variant wins: 'hades 2'
            canon = vmap[q]                     # must never lose to 'hades'
            return by_name[canon], canon
        # A bare pronoun ("it", "the") is a token-subset of some name and
        # scores 100 - refuse it so "play it" reaches the assistant.
        if len(q.split()) == 1 and len(q) <= 3:
            return None, None
        hits = process.extract(q, keys, scorer=fuzz.token_set_ratio, limit=3)
        if not hits or hits[0][1] < threshold:
            return None, None
        canon = vmap[hits[0][0]]
        for key, score, _ in hits[1:]:
            if vmap[key] != canon and score > hits[0][1] - margin:
                return None, None               # ambiguous across entries
        return by_name[canon], canon

    return resolve


def build_resolver(threshold, margin=5):
    """spoken -> (appid, canonical title) or (None, None), over installed games."""
    rows = library.load().get("installed", [])
    return _resolver_from({r["name"]: r["appid"] for r in rows if r.get("name")},
                          threshold, margin)


def build_collection_resolver(threshold, margin=5):
    """spoken -> (collection id, canonical name) or (None, None), over Big
    Picture collections. None when there are none yet."""
    rows = library.load().get("collections", [])
    return _resolver_from({r["name"]: r["id"] for r in rows
                           if r.get("name") and r.get("id")}, threshold, margin)
