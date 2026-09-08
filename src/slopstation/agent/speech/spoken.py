"""Strip citations from the assistant's answer before the TTS speaks it."""

from __future__ import annotations

import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

CITATION = re.compile(r"\s*\(\s*\[[^\]\n]*\]\([^)\s]*\)\s*\)")
LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
SOURCE_NAME = re.compile(r"[^\s/]+\.[a-z]{2,}", re.IGNORECASE)
PAREN_URL = re.compile(r"\s*\(\s*(?:https?://|www\.)[^)\s]*\s*\)")
BARE_URL = re.compile(r"\s*(?:https?://|www\.)\S+")
EMPTY_PAIR = re.compile(r"\s*[(\[]\s*[)\]]")
SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([,.;:!?])")
RUN_OF_SPACES = re.compile(r"[ \t]{2,}")


def _label(match):
    label = match.group(1).strip()
    return "" if SOURCE_NAME.fullmatch(label) else label


def spoken(text: str) -> str:
    """`text` without anything the TTS would read out as an address."""
    text = CITATION.sub("", text)
    text = LINK.sub(_label, text)
    text = PAREN_URL.sub("", text)
    text = BARE_URL.sub("", text)
    text = EMPTY_PAIR.sub("", text)
    text = SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    return RUN_OF_SPACES.sub(" ", text)


class SpokenText(BaseTextFilter):
    """Pipecat filters after sentence aggregation, so a citation arrives whole."""

    async def filter(self, text: str) -> str:
        return spoken(text)
