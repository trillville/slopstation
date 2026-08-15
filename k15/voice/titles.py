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
    """The forms of one title worth teaching the STT, most-specific first.

    A keyterm is matched against what the model EMITS, so it has to look like
    a transcript rather than a store page. Sending Steam's own string taught
    Flux nothing about "armored core six": ARMORED CORE VI FIRES OF RUBICON
    boosted a phrase nobody says, half-fired, and produced ARMORED CORSICS /
    ARMORED COSTS / ARMOR CORE 6 - 11 of 12 launches missed. spoken_form is
    the normalisation the grammar and the fuzzy resolver already agree on
    (and it emits digits, which is what numerals=true makes Flux write), so
    all three now describe a title the same way.

    Derived here rather than taken from variants() because the two want
    different things. variants() is the resolver's key space, where an extra
    key is free (ambiguity gets culled downstream); a keyterm is not free, it
    spends boost, and the set has to cover the SHORT name a person actually
    says even when Steam never writes it:

      a lone digit with title text after it ends the name proper, so
      'armored core 6 fires of rubicon' also teaches 'armored core 6' - the
      whole reason the launch kept missing. One digit only: 40,000 is a
      number, not a sequel, and cutting it yields 'warhammer 40'.

      the trailing-number strip ('hades 2' -> 'hades', which is also how the
      plain Hades gets covered) is skipped when what remains still ends in a
      digit, for the same reason."""
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


def _resolver_from(by_name, threshold, margin=5):
    """The fuzzy resolver over any {name: id} map. Fuzzy over the culled variant
    space; a near-tie between DIFFERENT entries (token_set_ratio scores subsets
    at 100, so 'warhammer' ties every 40K title) resolves to nothing rather than
    a coin flip - saying no beats picking wrong. Shared by installed-title and
    collection-name resolution: the machinery is identical, only the map's ids
    differ (appid vs collection id)."""
    if not by_name:
        return None
    vmap = {fuzzy_norm(v): canon
            for v, canon in variant_map(list(by_name)).items() if fuzzy_norm(v)}
    keys = list(vmap)
    from rapidfuzz import fuzz, process

    def resolve(spoken):
        q = fuzzy_norm(spoken)
        if q in vmap:                           # exact variant: no fuzz, no
            canon = vmap[q]                     # ambiguity ('hades 2' must
            return by_name[canon], canon        # never lose to 'hades')
        # A bare pronoun/stopword ("it", "the") is a token-subset of some
        # name and would score 100 on token_set_ratio - refuse short
        # single-token queries so "play it" falls through to the assistant.
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
    """spoken -> (collection id, canonical name) or (None, None), over the
    library's Big Picture collections (synced from the PC's `collections` verb).
    None when there are no collections yet (asleep PC, or none created)."""
    rows = library.load().get("collections", [])
    return _resolver_from({r["name"]: r["id"] for r in rows
                           if r.get("name") and r.get("id")}, threshold, margin)
