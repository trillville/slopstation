"""Define the assistant prompt and assemble its tools from the toolsets."""

import json
import time

from slopstation.agent.llm import prompts, toolsets
from slopstation.agent.llm.registry import AREAS, Registry, ToolContext

# tool spans; the module self-gates: REPL/bench are no-ops
from slopstation.agent.telemetry import sentry
from slopstation.agent.tools import library

# Every tool the assistant can ever offer, in the order the model sees them.
# A tool's description is its whole interface: the rules about a tool live
# with the tool (toolsets/*.py), the rules about behaviour in prompts.py.
REGISTRY = Registry(spec for module in toolsets.ALL for spec in module.SPECS)


def tools_map(offered=None):
    """The paragraph that tells the model what find_tools can reach: the
    default set is loaded, the rest is listed by area with a count. `offered`
    narrows to the tools the present services allow; None counts them all."""
    names = REGISTRY.names() if offered is None else list(offered)
    loaded = [n for n in names if REGISTRY.get(n).default]
    rest = REGISTRY.by_area([n for n in names if n not in loaded])
    if not rest:
        return ""
    areas = "; ".join(
        f"{area} ({len(specs)}): {AREAS[area]}" for area, specs in rest.items()
    )
    return (
        f"TOOLS: {len(loaded)} are loaded. find_tools reaches "
        f"{sum(len(s) for s in rest.values())} more - {areas}. When an ask is "
        "not covered by a loaded tool, call find_tools before answering that "
        "it cannot be done."
    )


def system_instruction(cfg, interface="voice", offered=None):
    """Build the system prompt from configuration and the game catalog."""
    voice = cfg["voice"]
    inputs = voice.get("inputs", {})
    # A day with no zone resolves toward UTC and dates an evening brief
    # tomorrow. Empty timezone is a normal deployment.
    tz = voice.get("location", {}).get("timezone")
    # The clock too, or the model searches the web for the time.
    tail = [
        f"It is {time.strftime('%H:%M')} on {time.strftime('%Y-%m-%d')}"
        + (f" in {tz}." if tz else " local time.")
    ]
    if inputs:
        gaming = next(
            (k for k, v in inputs.items() if v == cfg.get("tvGamingCmd")), None
        )
        tail.append(
            f"TV inputs: {', '.join(inputs)}"
            + (f"; '{gaming}' starts a session if none is running." if gaming else ".")
        )
    tail.append(
        f"Volume runs 0-{voice['volumeMax']}, higher requests are clamped - "
        "confirm the level the tool actually returns. Mute is a blind toggle "
        "with no readable state - say you toggled it, never claim on or off."
    )
    if voice["assistantWebSearch"]:
        tail.append(prompts.WEB_SEARCH_RULE)
    tools = tools_map(offered)
    if tools:
        tail.append(tools)
    style = prompts.TEXT_STYLE if interface == "text" else prompts.VOICE_STYLE
    input_rule = "" if interface == "text" else "\n\n" + prompts.VOICE_INPUT_RULE
    return (
        style + input_rule + "\n\n" + prompts.RULES + " " + " ".join(tail) + "\n\n"
        "CATALOG (appid|name|tags|genres|hours|lastPlayed YYYY-MM-DD or "
        "never|inst[:YYYY-MM-DD last install or update]/notinst|controller "
        "full/partial/none/?):\n" + "\n".join(library.catalog_lines())
    )


class Toolkit:
    """One conversation's tools: what is offered, what is loaded, how to call.

    Offered is every registry tool whose services are present. Loaded starts
    as the default set and grows when find_tools matches; it never shrinks
    inside a conversation. Both backends render the loaded set on every
    request, so a tool found mid-turn is callable on the next one. `on_load`
    lets the voice lane push the new list into its Pipecat context."""

    def __init__(
        self,
        dispatch,
        log,
        operations=None,
        on_stop_listening=None,
        voice=None,
        steam=None,
        media=None,
        on_load=None,
    ):
        self.registry = REGISTRY
        self.log = log
        self.on_load = on_load
        self.ctx = ToolContext(
            dispatch=dispatch,
            log=log,
            operations=operations,
            on_stop_listening=on_stop_listening,
            voice=voice,
            steam=steam,
            media=media,
        )
        self.ctx.toolkit = self
        self.offered = [s.name for s in REGISTRY.offered(self.ctx.services())]
        offered = set(self.offered)
        self.impls = {}
        for module in toolsets.ALL:
            # A toolset whose service is absent still has to build cleanly: it
            # closes over None and is never called.
            for name, fn in module.impls(self.ctx).items():
                if name in offered:
                    self.impls[name] = fn
        self.loaded = [n for n in self.offered if REGISTRY.get(n).default]

    def load(self, names):
        """Load offered tools by name; returns the ones newly loaded, in
        registry order, and tells the voice context when anything changed."""
        wanted = {n for n in names if n in self.impls and n not in self.loaded}
        if not wanted:
            return []
        self.loaded = [n for n in self.offered if n in self.loaded or n in wanted]
        if self.on_load is not None:
            self.on_load()
        return [n for n in self.loaded if n in wanted]

    def render(self, provider):
        if provider == "openai":
            return REGISTRY.openai_tools(self.loaded)
        return REGISTRY.anthropic_tools(self.loaded)

    def call(self, name, args):
        fn = self.impls.get(name)
        if fn is None:
            return {"ok": False, "error": f"there is no tool called {name}"}
        return fn(args)

    def function_schemas(self, log=None):
        """Pipecat schemas for the LOADED tools."""
        return function_schemas(
            {n: self.impls[n] for n in self.loaded}, log or self.log
        )


class StaticTools:
    """A plain name->fn dict wearing the Toolkit interface, for the REPL and
    tests. Renders the registry entry of every name it holds."""

    def __init__(self, impls):
        self.impls = dict(impls)
        self.loaded = [n for n in REGISTRY.names() if n in self.impls]

    def render(self, provider):
        if provider == "openai":
            return REGISTRY.openai_tools(self.loaded)
        return REGISTRY.anthropic_tools(self.loaded)

    def call(self, name, args):
        fn = self.impls.get(name)
        if fn is None:
            return {"ok": False, "error": f"there is no tool called {name}"}
        return fn(args)


def as_tools(tools):
    """Accept a Toolkit-shaped object or a bare impls dict."""
    return StaticTools(tools) if isinstance(tools, dict) else tools


def tool_impls(
    dispatch,
    log,
    operations=None,
    on_stop_listening=None,
    voice=None,
    steam=None,
    media=None,
):
    """Every callable implementation for the supplied services, as a dict.
    The Toolkit is the conversation-shaped view of the same thing."""
    return Toolkit(
        dispatch,
        log,
        operations=operations,
        on_stop_listening=on_stop_listening,
        voice=voice,
        steam=steam,
        media=media,
    ).impls


def record_tool_call(name, args, out, log=None):
    """Record a tool call in Sentry and the local log."""
    try:
        sentry.tool_span(name, json.dumps(args)[:2000], json.dumps(out)[:2000])
    except Exception:
        pass
    if log:
        ok = out.get("ok") if isinstance(out, dict) else None
        log("tool_call", tool=name, ok=ok, args=json.dumps(args)[:300])


def function_schemas(impls, log):
    """Build Pipecat schemas that run blocking tools in worker threads."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

    def wrap(name, fn):
        async def handler(params):
            args = dict(params.arguments)
            try:
                out = await asyncio.to_thread(fn, args)
            except Exception as e:
                # Always return a result so a tool exception cannot hang the turn.
                log.error("tool_error", tool=name, err=repr(e))
                out = {
                    "ok": False,
                    "error": "that didn't go through - something upstream failed",
                }
            # After the call, so the span carries the RESULT. The await above
            # does not lose the OTel context (contextvars are per-task), so
            # this still parents onto Pipecat's llm span.
            record_tool_call(name, args, out, log)
            acknowledgment = (
                out.get("acknowledgment") if isinstance(out, dict) else None
            )
            end_turn = isinstance(out, dict) and bool(out.get("end_turn"))
            if end_turn:
                # The session ends on this call: no goodbye to a closing mic.
                await params.result_callback(
                    out, properties=FunctionCallResultProperties(run_llm=False)
                )
            elif acknowledgment:

                async def speak():
                    await params.pipeline_worker.queue_frame(
                        TTSSpeakFrame(str(acknowledgment))
                    )

                properties = FunctionCallResultProperties(
                    run_llm=False, on_context_updated=speak
                )
                await params.result_callback(out, properties=properties)
            else:
                await params.result_callback(out)

        return handler

    # Render only tools present in `impls` - the schema half of the gating.
    return [
        FunctionSchema(
            name=spec.name,
            description=spec.description,
            properties=spec.properties,
            required=list(spec.required),
            handler=wrap(spec.name, impls[spec.name]),
        )
        for spec in REGISTRY
        if spec.name in impls
    ]


# `names` filters to the tools present in a given impls set, so a renderer
# can't offer a tool that isn't callable; None renders every spec.
def anthropic_tools(names=None):
    return REGISTRY.anthropic_tools(names)


def openai_tools(names=None):
    return REGISTRY.openai_tools(names)


def _user_location(voice):
    """Non-empty location fields -> the 'approximate' user_location dict, or
    None when nothing is set. Both providers accept the identical shape."""
    loc = {k: v for k, v in voice["location"].items() if v}
    return {"type": "approximate", **loc} if loc else None


def server_tools(voice, provider):
    """Provider-native tools (the provider executes them; nothing in
    tool_impls), appended next to the registry renders. Today: web search
    behind config.assistantWebSearch. Anthropic caps calls via max_uses;
    OpenAI has no cap knob, hence search_context_size low."""
    if not voice["assistantWebSearch"]:
        return []
    if provider == "openai":
        tool = {"type": "web_search", "search_context_size": "low"}
    else:
        tool = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": voice["assistantSearchMaxUses"],
        }
    loc = _user_location(voice)
    if loc:
        tool["user_location"] = loc
    return [tool]


# Both keys stay populated in config so flipping assistantProvider is the
# whole A/B, and neither lane hides behind a default.
MODEL_KEY = {"anthropic": "assistantModelAnthropic", "openai": "assistantModelOpenai"}
PROVIDER_KEY = {"anthropic": "anthropicApiKey", "openai": "openaiApiKey"}


def default_model(voice, provider):
    return voice[MODEL_KEY[provider]]
