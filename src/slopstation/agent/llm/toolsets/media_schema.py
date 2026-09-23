"""Schemas shared by media lookup, browsing, and mutation tools."""

from functools import partial

from slopstation.agent.llm.registry import ToolSpec

KIND = {"type": "string", "enum": ["movie", "series"]}
CATALOG_ID = {
    "type": "integer",
    "description": "TMDB movie id or TVDB series id from find_media",
}

media_spec = partial(ToolSpec, area="media", default=False, needs=("media",))
