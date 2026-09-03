"""The vocabulary handed to Flux: installed titles, collection names and the
tag/genre words people ask with, each in the form Flux will hear it.
"""

from slopstation import logbook
from slopstation.agent.tools import library, titles

log = logbook.logger("voice")

# Deepgram refuses >100 terms at the handshake (HTTP 400); weights are ignored.
MAX_KEYTERMS = 100

# Tag/genre slots - the jargon half of the vocabulary. Deepgram's own guidance
# is 20-50 terms and no generic words.
QUERY_TERM_SLOTS = 30

# Ordinary English, in spoken_form. Flux gets these right unprompted, so a slot
# spent here is a slot not spent on a coined word it does get wrong.
GENERIC_TERMS = frozenset(
    {
        "2d",
        "3d",
        "action",
        "adventure",
        "anime",
        "arcade",
        "atmospheric",
        "base building",
        "beautiful",
        "building",
        "casual",
        "character customization",
        "choices matter",
        "cinematic",
        "city builder",
        "classic",
        "colorful",
        "combat",
        "comedy",
        "competitive",
        "crafting",
        "cute",
        "dark",
        "dark fantasy",
        "difficult",
        "driving",
        "dungeon crawler",
        "early access",
        "economy",
        "education",
        "epic",
        "exploration",
        "family friendly",
        "fantasy",
        "fast paced",
        "female protagonist",
        "fighting",
        "first person",
        "flight",
        "free to play",
        "funny",
        "future",
        "gore",
        "grand strategy",
        "great soundtrack",
        "historical",
        "horror",
        "indie",
        "local co op",
        "loot",
        "magic",
        "management",
        "massively multiplayer",
        "mature",
        "medieval",
        "military",
        "mining",
        "modern",
        "multiplayer",
        "music",
        "mystery",
        "mythology",
        "nature",
        "nudity",
        "online",
        "online co op",
        "open world",
        "physics",
        "platformer",
        "point click",
        "political",
        "psychological",
        "puzzle",
        "racing",
        "realistic",
        "relaxing",
        "replay value",
        "resource management",
        "retro",
        "romance",
        "sandbox",
        "sci fi",
        "science",
        "sexual content",
        "shooter",
        "short",
        "silly",
        "simulation",
        "singleplayer",
        "software",
        "space",
        "sports",
        "stealth",
        "story",
        "story rich",
        "strategy",
        "survival",
        "tactical",
        "third person",
        "trading",
        "turn based strategy",
        "turn based tactics",
        "underwater",
        "utilities",
        "violent",
        "war",
        "zombies",
    }
)


def _dedupe(terms):
    seen, out = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def query_keyterms(limit=QUERY_TERM_SLOTS):
    """The words used to ask ABOUT games, in the form Flux emits them: every
    term goes through spoken_form, never SteamSpy's punctuation ('rogue-like',
    'souls-like', 'co-op')."""
    return [
        t
        for t in _dedupe(titles.spoken_form(x) for x in library.query_terms(None))
        if t not in GENERIC_TERMS
    ][:limit]


def load_titles(count, rows=None):
    """Installed titles by recency, spelled as Steam writes them."""
    if rows is None:
        rows = library.load().get("installed", [])
    rows = sorted(rows, key=lambda r: r.get("lastPlayed", 0), reverse=True)
    return [
        r["name"]
        for r in rows
        if r.get("name") and r.get("appid") not in library.NOT_GAMES
    ][:count]


def stt_keyterms(voice, wake_phrase, catalog=None):
    """Everything Flux is told to expect, in the form it will hear it:
    titles, collection names, tag/genre words.

    Order is the budget policy - the cap truncates the tail, and titles come
    first because they carry every observed launch while collection names and
    query words carry none. Truncation is logged out loud - a silently short
    list reads as full coverage."""
    catalog = catalog or library.Catalog.load()
    terms = [wake_phrase]
    for name in load_titles(voice["keytermCount"], catalog.installed):
        terms += titles.keyterm_forms(name)
    terms += [
        titles.spoken_form(c["name"]) for c in catalog.collections if c.get("name")
    ]
    terms += query_keyterms()

    out = _dedupe(terms)
    if len(out) > MAX_KEYTERMS:
        log.warn(
            "keyterms_capped",
            kept=MAX_KEYTERMS,
            dropped=len(out) - MAX_KEYTERMS,
            first_dropped=out[MAX_KEYTERMS],
        )
        out = out[:MAX_KEYTERMS]
    return out
