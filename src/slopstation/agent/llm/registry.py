"""Describe every assistant tool once: schema, risk, area, keywords, default."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from slopstation.agent.llm.confirm import ConfirmGate

Risk = Literal["read", "act", "destructive"]
RISKS: tuple[Risk, ...] = ("read", "act", "destructive")

# The areas find_tools will name on a miss, so the model can search again
# with better words. A spec outside these is a typo.
AREAS = ("session", "steam", "media", "house")

# What a tool needs before it is offered: an operations store, a media
# service, or the steam data lane (which config can switch off). A tool with
# no needs is always offered.
SERVICES = ("operations", "media", "steam_data")


@dataclass(frozen=True)
class ToolSpec:
    """One tool as the model sees it, plus what the registry knows about it."""

    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    risk: Risk
    area: str
    # find_tools searches these before the description, so they carry the
    # synonyms and spoken forms a description would not repeat.
    keywords: tuple[str, ...]
    default: bool = True
    needs: tuple[str, ...] = ()
    # A paged tool lists rows: it takes a limit, returns the count first and a
    # capped page. The registry test holds it to that.
    paged: bool = False

    def anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
            },
        }

    def openai(self) -> dict[str, Any]:
        # Responses API tool shape is FLAT (name/parameters at top level) - the
        # nested {"function": {...}} form is chat-completions only.
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
            },
        }


@dataclass
class ToolContext:
    """The services a toolset's implementations close over."""

    dispatch: Any
    log: Any
    operations: Any = None
    on_stop_listening: Callable[[], None] | None = None
    voice: dict | None = None
    steam: Any = None
    media: Any = None
    gate: ConfirmGate = field(default_factory=ConfirmGate)

    def services(self) -> frozenset[str]:
        """Which of SERVICES are present, so specs can be gated by `needs`."""
        have = set()
        if self.operations is not None:
            have.add("operations")
        if self.media is not None:
            have.add("media")
        if self.voice is None or self.voice.get("steamDataTools", True):
            have.add("steam_data")
        return frozenset(have)


class Registry:
    """Every ToolSpec, in the order the model sees them."""

    def __init__(self, specs: Iterable[ToolSpec]):
        self._specs: list[ToolSpec] = list(specs)
        seen: set[str] = set()
        for spec in self._specs:
            if spec.name in seen:
                raise ValueError(f"duplicate tool {spec.name}")
            seen.add(spec.name)
            if spec.risk not in RISKS:
                raise ValueError(f"{spec.name}: risk {spec.risk!r} not in {RISKS}")
            if spec.area not in AREAS:
                raise ValueError(f"{spec.name}: area {spec.area!r} not in {AREAS}")
            unknown = set(spec.needs) - set(SERVICES)
            if unknown:
                raise ValueError(f"{spec.name}: needs {sorted(unknown)} unknown")

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, name: str) -> ToolSpec:
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def names(self) -> list[str]:
        return [s.name for s in self._specs]

    def offered(self, services: frozenset[str]) -> list[ToolSpec]:
        """The specs whose needs the present services satisfy."""
        return [s for s in self._specs if set(s.needs) <= services]

    # `names` filters to the tools present in a given impls set, so a renderer
    # can't offer a tool that isn't callable; None renders every spec.
    def anthropic_tools(self, names: Iterable[str] | None = None) -> list[dict]:
        keep = None if names is None else set(names)
        return [s.anthropic() for s in self._specs if keep is None or s.name in keep]

    def openai_tools(self, names: Iterable[str] | None = None) -> list[dict]:
        keep = None if names is None else set(names)
        return [s.openai() for s in self._specs if keep is None or s.name in keep]
