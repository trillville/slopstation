"""Find the tools an ask needs, by words alone.

Lexical on purpose: the corpus is under a hundred short records and every
query is a phrase the model writes, so keyword hits with a fuzzy fallback
retrieve as well as anything heavier, offline and deterministically. The
scorer sits behind search() so it can be swapped without touching a tool.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from slopstation.agent.llm.registry import Registry, ToolSpec

# Words that carry no signal about which tool is wanted.
STOPWORDS = frozenset(
    "a an and are be can could do does for from get how i in is it me my of on "
    "or please show tell that the this to want what which with would you your".split()
)
TOP_N = 5
# Below this a match is noise; the caller lists the areas instead.
FLOOR = 2.0
# A keyword phrase counts as present when it fuzzes this close to the query.
FUZZ_MIN = 82


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


def score(query: str, spec: ToolSpec) -> float:
    """Keywords first, the name next, the description last."""
    q = query.lower()
    qtok = set(tokens(query))
    if not qtok:
        return 0.0
    total = 0.0
    for phrase in spec.keywords:
        p = phrase.lower()
        if p in q:
            total += 3.0
            continue
        ratio = fuzz.partial_ratio(p, q)
        if ratio >= FUZZ_MIN:
            total += 2.0 * ratio / 100
        else:
            # A one-word keyword that survives tokenising still counts.
            total += 1.0 * len(qtok & set(tokens(p)))
    total += 2.0 * len(qtok & set(spec.name.split("_")))
    total += 0.3 * len(qtok & set(tokens(spec.description)))
    return round(total, 3)


def search(
    registry: Registry, query: str, exclude: set[str] | None = None, limit: int = TOP_N
) -> list[tuple[ToolSpec, float]]:
    """The best `limit` tools for the query above the floor, best first, ties
    broken by registry order so the result is deterministic."""
    exclude = exclude or set()
    ranked = [
        (i, spec, score(query, spec))
        for i, spec in enumerate(registry)
        if spec.name not in exclude
    ]
    ranked = [r for r in ranked if r[2] >= FLOOR]
    ranked.sort(key=lambda r: (-r[2], r[0]))
    return [(spec, sc) for _, spec, sc in ranked[:limit]]


def summary(spec: ToolSpec) -> str:
    """The first sentence of the description: what the tool is for."""
    first = re.split(r"(?<=[.!?])\s", spec.description.strip(), maxsplit=1)[0]
    return re.sub(r"\s+", " ", first)
