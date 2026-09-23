"""Describe every assistant tool once, and bind it to what runs it.

A ToolSpec is the tool as the model sees it plus what the registry knows
about it. A toolset module lists its specs and, inside `impls(ctx)`, binds
each to a function through `Bindings`: the function's name is the tool's
name, so there is no second list to drift, and a spec without a function
(or a function without a spec) fails when the toolkit is built, not in a
test. A destructive spec can only be bound as a `Plan`-returning function:
the binding owns the ask, the dry run and the later-turn yes, so a new
destructive tool cannot skip the gate."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from slopstation.agent.llm.confirm import ConfirmGate

Risk = Literal["read", "act", "destructive"]
RISKS: tuple[Risk, ...] = ("read", "act", "destructive")

# The areas, each with the gloss the system prompt and find_tools use to say
# what lives there. A spec outside these is a typo.
AREAS = {
    "session": "the session, the TV, the mic, and Big Picture on the PC",
    "steam": "Steam: the catalog, the store, playtime, friends, downloads",
    "media": "movies and TV through Radarr and Sonarr, and the torrents under them",
    "storage": "disk space and files under the media root",
    "house": "Slopstation itself: operations, lanes, logs, settings",
    "api": "direct calls to any service's API when no tool covers the ask",
}

# What a tool needs before it is offered: an operations store, a media
# service (and the qBittorrent and Prowlarr clients it may carry), the Steam
# account session, or the steam data lane (which config can switch off). A
# tool with no needs is always offered.
SERVICES = (
    "operations",
    "media",
    "torrents",
    "prowlarr",
    "steam_account",
    "steam_data",
)


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
    # What the voice lane says when this tool has kept the user waiting (the
    # busy tone): a few words in the present tense, "checking Steam". {game}
    # is filled with the title behind the appid argument. None means the
    # lane's own fallback, the earcon or the configured phrase. Only tools
    # that reach a network or the PC need one; a local read never fires it.
    busy: str | None = None

    def _render(self, schema_key: str) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            schema_key: {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
            },
        }

    def anthropic(self) -> dict[str, Any]:
        return self._render("input_schema")

    def openai(self) -> dict[str, Any]:
        # Responses uses a flat tool shape, unlike Chat Completions.
        return {"type": "function", **self._render("parameters")}


@dataclass(frozen=True)
class Plan:
    """What a destructive tool hands back once it has resolved its target and
    found nothing wrong: the binding asks `ask` on the first call, previews
    `preview` on a dry run, and runs `act` on a later turn's yes. `scope`
    identifies the question, so a different target is a different ask."""

    scope: tuple
    ask: str  # spoken as-is; "" hands the turn back to the model
    act: Callable[[], dict]
    preview: str
    confirm: str = ""  # the literal a text lane shows instead of `ask`


# The utterance a running tool was called under. The grammar gate replaces
# dispatch.utterance with each transcript while a tool may still be running on
# a worker thread, so a tool reads this pin, not the live value. None outside a
# call (REPL, tests).
_UTTERANCE: contextvars.ContextVar[tuple[str | None, str] | None] = (
    contextvars.ContextVar("utterance", default=None)
)


@contextlib.contextmanager
def utterance_snapshot(dispatch):
    """Pin dispatch.utterance for the tool about to run."""
    live = getattr(dispatch, "utterance", None)
    token = _UTTERANCE.set(
        (getattr(live, "turn", None), getattr(live, "asked", None) or "")
    )
    try:
        yield
    finally:
        _UTTERANCE.reset(token)


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
    # The Toolkit that owns this context, for the tools that act on the tool
    # set itself (find_tools). Set by the Toolkit after construction.
    toolkit: Any = None

    def turn(self) -> str | None:
        """Turn id of the utterance this tool runs under. None means no
        utterance, and the confirmation gate refuses."""
        pinned = _UTTERANCE.get()
        if pinned is not None:
            return pinned[0]
        return getattr(getattr(self.dispatch, "utterance", None), "turn", None)

    def asked(self) -> str:
        """Words of the utterance this tool runs under, or ""."""
        pinned = _UTTERANCE.get()
        if pinned is not None:
            return pinned[1]
        return getattr(getattr(self.dispatch, "utterance", None), "asked", None) or ""

    def preview(self, action: str) -> dict | None:
        """On a dry run, the answer a mutation gives instead of acting; None
        means go ahead. `action` reads as the rest of "would ..."."""
        if not self.dispatch.dry_run:
            return None
        self.log("dry_run_would", action=action)
        return {"ok": True, "dry_run": True, "detail": f"would {action}"}

    def confirm(self, tool: str, scope: tuple, ask: dict, act: Callable[[], dict]):
        """Ask first, act on a later yes, spend the ask on success. `ask` is
        the payload the refusal carries (a spoken `acknowledgment`, or the
        literal a text lane reads back). Anything not `ok` from `act` keeps
        the ask armed, so a retry does not ask twice."""
        if not self.gate.confirmed(scope, self.turn()):
            self.log.warn("tool_refused", tool=tool, reason="unconfirmed")
            return {"ok": False, **ask}
        out = act()
        if isinstance(out, dict) and out.get("ok"):
            self.gate.done(scope)
        return out

    def services(self) -> frozenset[str]:
        """Which of SERVICES are present, so specs can be gated by `needs`."""
        have = set()
        if self.operations is not None:
            have.add("operations")
        if self.media is not None:
            have.add("media")
            if getattr(self.media, "qbit", None) is not None:
                have.add("torrents")
            if getattr(self.media, "prowlarr", None) is not None:
                have.add("prowlarr")
        if self.steam is not None:
            have.add("steam_account")
        if self.voice is None or self.voice.get("steamDataTools", True):
            have.add("steam_data")
        return frozenset(have)


class Bindings:
    """One toolset's specs bound to their functions, by name.

        bind = Bindings(ctx, SPECS)

        @bind
        def list_torrents(args): ...

        @bind.destructive
        def delete_torrent(args): ...  # returns a Plan, or an error dict

        return bind.impls()

    `impls()` refuses a spec left unbound."""

    def __init__(self, ctx: ToolContext, specs: Iterable[ToolSpec]):
        self.ctx = ctx
        self._specs = {s.name: s for s in specs}
        self._impls: dict[str, Callable[[dict], dict]] = {}

    def _add(self, fn, gated: bool):
        name = fn.__name__
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"{name} has no ToolSpec in this toolset")
        if name in self._impls:
            raise ValueError(f"{name} is bound twice")
        if (spec.risk == "destructive") != gated:
            how = "bind.destructive" if spec.risk == "destructive" else "bind"
            raise ValueError(f"{name} is {spec.risk}: bind it with @{how}")
        self._impls[name] = fn

    def __call__(self, fn):
        self._add(fn, gated=False)
        return fn

    def destructive(self, fn):
        """Bind a destructive tool. The function validates and resolves, then
        returns a Plan; the binding runs the ask-then-act lifecycle around it.
        An error dict (or any non-Plan) passes straight through."""
        name, ctx = fn.__name__, self.ctx

        def run(args):
            plan = fn(args)
            if not isinstance(plan, Plan):
                return plan
            if dry := ctx.preview(plan.preview):
                return dry
            if plan.ask:
                ask = {"acknowledgment": plan.ask}
            else:
                ask = {
                    "confirm": plan.confirm,
                    "error": "not run yet: read this request back to the user in "
                    "plain words, and call again unchanged once they say yes",
                }
            return ctx.confirm(name, plan.scope, ask, plan.act)

        run.__name__ = name
        self._add(run, gated=True)
        return run

    def impls(self) -> dict[str, Callable[[dict], dict]]:
        missing = sorted(set(self._specs) - set(self._impls))
        if missing:
            raise ValueError(f"no implementation bound for {missing}")
        return dict(self._impls)


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

    def by_area(self, names: Iterable[str]) -> dict[str, list[ToolSpec]]:
        """The named specs grouped by area, in AREAS order."""
        keep = set(names)
        out: dict[str, list[ToolSpec]] = {area: [] for area in AREAS}
        for s in self._specs:
            if s.name in keep:
                out[s.area].append(s)
        return {a: specs for a, specs in out.items() if specs}

    def select(self, names: Iterable[str]) -> list[ToolSpec]:
        """The specs to render, in the order given: a toolkit's loaded set is
        defaults first, then what was found, in the order it was found, so the
        rendered prefix stays cache-stable."""
        return [self.get(n) for n in names]

    def anthropic_tools(self, names: Iterable[str]) -> list[dict]:
        return [s.anthropic() for s in self.select(names)]

    def openai_tools(self, names: Iterable[str]) -> list[dict]:
        return [s.openai() for s in self.select(names)]
