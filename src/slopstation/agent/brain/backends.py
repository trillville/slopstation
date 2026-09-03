"""Each provider's plain SDK loop - one turn, with its tool loop, to reply text
- and the --text REPL that drives one locally.

The backends are production: interfaces/text.py builds one per LAN chat session
from BACKENDS, so this is the conversation path for text and MCP. The REPL
wires only the base tool set, so it is not tool-for-tool with a live session;
nothing here is on the wake path.
"""

import json
import time
from collections.abc import Callable

from slopstation import config
from slopstation.agent.brain import assistant, llm_audit
from slopstation.agent.telemetry import sentry, traces

# Both SDKs default to 600 s per attempt plus retries. remote.py abandons its
# forward at 280 s and text.py holds the session lock for the whole turn, so a
# stalled attempt has to die before every caller's patience, not after.
LLM_TIMEOUT_S = 90
LLM_MAX_RETRIES = 1


# --- provider backends: one turn (with its tool loop) -> reply text -----------
# Each backend holds its own conversation state.


class Backend:
    """What the REPL and the trace need from either provider."""

    model: str
    messages: list
    server_tools: list
    cache_note: str

    def turn(self, system_text, user_text, impls) -> str:
        raise NotImplementedError


class AnthropicBackend(Backend):
    key = assistant.PROVIDER_KEY["anthropic"]

    def __init__(self, secrets, model, effort=None, voice=None):
        import anthropic

        self.client = anthropic.Anthropic(
            api_key=secrets[self.key],
            timeout=LLM_TIMEOUT_S,
            max_retries=LLM_MAX_RETRIES,
        )
        self.model = model
        self.messages = []
        self.cache_note = ""
        self.server_tools = assistant.server_tools(voice, "anthropic") if voice else []

    @sentry.agent("assistant")
    def turn(self, system_text, user_text, impls):
        # cache_control on the system block caches tools+system together: a
        # breakpoint covers everything before it, and the render order is
        # tools -> system -> messages. Small models have a
        # minimum cacheable prefix (4096 tokens on Haiku 4.5) below which the
        # marker silently does nothing; the REPL's cache w/r makes that visible.
        system = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        self.messages.append({"role": "user", "content": user_text})
        spoken = []  # text carried across pause_turn continuations
        while True:
            tools = assistant.anthropic_tools(set(impls)) + self.server_tools
            # The span closes before the tool loop below, so tool spans land
            # beside it under the agent span rather than inside it - Sentry's
            # documented hierarchy.
            with sentry.chat_span(
                "anthropic",
                self.model,
                system=system_text,
                messages=self.messages,
                tools=tools,
            ) as span:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    system=system,  # type: ignore[arg-type]
                    messages=self.messages,
                    tools=tools,
                )
                u = resp.usage
                text = " ".join(b.text for b in resp.content if b.type == "text")
                # getattr throughout: every one of these is read ONLY for
                # the span, and arguments are evaluated before response() can
                # swallow anything - so a field a provider stopped sending
                # would kill the turn rather than cost a span attribute.
                usage = {
                    "input": getattr(u, "input_tokens", None),
                    "output": getattr(u, "output_tokens", None),
                    "cache_read": getattr(u, "cache_read_input_tokens", None),
                    "cache_write": getattr(u, "cache_creation_input_tokens", None),
                }
                span.response(
                    model=getattr(resp, "model", None), output=text, usage=usage
                )
            self.cache_note = (
                f"cache w{getattr(u, 'cache_creation_input_tokens', 0) or 0}"
                f"/r{getattr(u, 'cache_read_input_tokens', 0) or 0}"
            )
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                # Contract: re-send the partial assistant content as-is and
                # let the model continue. Server tool blocks need no
                # client-side result; only the accumulated text matters.
                if text:
                    spoken.append(text)
                continue
            if resp.stop_reason != "tool_use":
                return " ".join(spoken + [text]) if spoken else text
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    out = impls[b.name](dict(b.input))
                    print(f"  [tool] {b.name}({dict(b.input)}) -> {out}")
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": json.dumps(out),
                        }
                    )
            self.messages.append({"role": "user", "content": results})


class OpenAIBackend(Backend):
    """Responses API: reasoning and tool calls coexist here, unlike the legacy
    chat-completions endpoint. State is server-side via previous_response_id,
    which also threads reasoning items across tool calls."""

    key = assistant.PROVIDER_KEY["openai"]

    def __init__(self, secrets, model, effort="low", voice=None):
        import openai

        self.client = openai.OpenAI(
            api_key=secrets[self.key],
            timeout=LLM_TIMEOUT_S,
            max_retries=LLM_MAX_RETRIES,
        )
        self.model = model
        self.effort = effort  # none|minimal|low|medium|high (model-dep)
        self.prev = None
        self.cache_note = ""
        self.messages = []  # trace mirror; real state is server-side
        self.server_tools = assistant.server_tools(voice, "openai") if voice else []

    @sentry.agent("assistant")
    def turn(self, system_text, user_text, impls):
        self.messages.append({"role": "user", "content": user_text})
        pending = [{"role": "user", "content": user_text}]
        while True:
            tools = assistant.openai_tools(set(impls)) + self.server_tools
            with sentry.chat_span(
                "openai", self.model, system=system_text, messages=pending, tools=tools
            ) as span:
                resp = self.client.responses.create(  # type: ignore[call-overload]
                    model=self.model,
                    instructions=system_text,
                    input=pending,
                    tools=tools,
                    reasoning={"effort": self.effort},
                    max_output_tokens=1500,
                    previous_response_id=self.prev,
                )
                u, det = resp.usage, getattr(resp.usage, "input_tokens_details", None)
                out_det = getattr(u, "output_tokens_details", None)
                usage = {  # getattr for the same reason as above
                    "input": getattr(u, "input_tokens", None),
                    "output": getattr(u, "output_tokens", None),
                    "cache_read": getattr(det, "cached_tokens", None),
                    "reasoning": getattr(out_det, "reasoning_tokens", None),
                }
                span.response(
                    model=getattr(resp, "model", None),
                    output=getattr(resp, "output_text", None),
                    usage=usage,
                )
            self.prev = resp.id
            self.cache_note = (
                f"cache r{getattr(det, 'cached_tokens', 0) or 0}" if det else ""
            )
            # Server-side searches are ITEMS in resp.output, not function
            # calls: filtering to function_call drops them entirely and the
            # trace then carries no evidence a lookup happened.
            for o in resp.output:
                hit = llm_audit.search_item(o)
                if hit:
                    self.messages.append(
                        {
                            "role": "tool",
                            "name": hit[0],
                            "query": hit[1],
                            "status": getattr(o, "status", None),
                        }
                    )
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                self.messages.append({"role": "assistant", "content": resp.output_text})
                return resp.output_text
            pending = []
            for c in calls:
                args = json.loads(c.arguments or "{}")
                out = impls[c.name](args)
                print(f"  [tool] {c.name}({args}) -> {out}")
                self.messages.append(
                    {"role": "tool", "name": c.name, "args": args, "out": out}
                )
                pending.append(
                    {
                        "type": "function_call_output",
                        "call_id": c.call_id,
                        "output": json.dumps(out),
                    }
                )


BACKENDS: dict[str, Callable[..., Backend]] = {
    "anthropic": AnthropicBackend,
    "openai": OpenAIBackend,
}


def repl(cfg, secrets, log, dry_run=True, provider=None, model=None, effort=None):
    """--text mode: type transcripts, see replies + tool calls + latency. The
    voice pipeline's system prompt and tool schemas, but only the base tool set
    - no operations, media or steam. Pick with --provider anthropic|openai
    [--model <id>] [--effort none|low|medium|high]."""
    from slopstation.agent.brain.dispatch import Dispatch

    provider = provider or cfg["voice"]["assistantProvider"]
    if provider not in BACKENDS:
        print(f"unknown provider '{provider}' - one of {list(BACKENDS)}")
        return 2
    keyname = assistant.PROVIDER_KEY[provider]
    if not config.real_key(secrets.get(keyname)):
        print(f"{keyname} is a placeholder - add it to secrets.json for {provider}")
        return 1

    impls = assistant.tool_impls(Dispatch(cfg, log, dry_run=dry_run), log)
    effort = effort or cfg["voice"]["assistantReasoningEffort"]
    backend = BACKENDS[provider](
        secrets,
        model or assistant.default_model(cfg["voice"], provider),
        effort=effort,
        voice=cfg["voice"],
    )
    system_text = assistant.system_instruction(cfg)
    tag = f"{provider}/{backend.model}"
    if provider == "openai":
        tag += f" effort={effort}"
    if backend.server_tools:
        tag += " +websearch"
    print(f"assistant REPL - {tag}, dry_run={dry_run}. Empty line to quit.")
    try:
        while True:
            try:
                q = input("you> ").strip()
            except EOFError:
                break
            if not q:
                break
            t0 = time.time()
            try:
                text = backend.turn(system_text, q, impls)
            except Exception as e:
                # A bad knob value (e.g. an unsupported reasoning effort) or a
                # transient API error shouldn't kill the REPL session.
                print(f"API error ({time.time() - t0:.1f}s)> {e}")
                continue
            note = f", {backend.cache_note}" if backend.cache_note else ""
            print(f"assistant ({time.time() - t0:.1f}s{note})> {text}")
    finally:
        # Save the transcript on every exit path, Ctrl-C included.
        traces.save(
            f"repl-{provider}",
            backend.messages,
            {"model": backend.model, "dry_run": dry_run},
        )
    return 0
