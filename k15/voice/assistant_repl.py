"""Each provider's plain SDK loop (one turn, with its tool loop, to reply text)
and the --text REPL that drives it. The backends are production: text_interface
builds one per LAN chat session from BACKENDS. The REPL wires only the base
tool set, so it is not tool-for-tool with a live session; nothing here is on
the wake path.
"""
import json
import time

import assistant
import cglib
import llm_audit
import traces


# --- provider backends: one turn (with its tool loop) -> reply text -----------
# Each backend holds its own conversation state.


class AnthropicBackend:
    key = assistant.PROVIDER_KEY["anthropic"]

    def __init__(self, secrets, model, effort=None, voice=None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=secrets[self.key])
        self.model = model
        self.messages = []
        self.cache_note = ""
        self.server_tools = assistant.server_tools(voice, "anthropic") if voice else []

    def turn(self, system_text, user_text, impls):
        # cache_control on the system block caches tools+system together: a
        # breakpoint covers everything before it, and the render order is
        # tools -> system -> messages. Small models have a
        # minimum cacheable prefix (4096 tokens on Haiku 4.5) below which the
        # marker silently does nothing; the REPL's cache w/r makes that visible.
        system = [{"type": "text", "text": system_text,
                   "cache_control": {"type": "ephemeral"}}]
        self.messages.append({"role": "user", "content": user_text})
        spoken = []          # text carried across pause_turn continuations
        while True:
            resp = self.client.messages.create(
                model=self.model, max_tokens=400, system=system,
                messages=self.messages,
                tools=assistant.anthropic_tools(set(impls)) + self.server_tools)
            u = resp.usage
            self.cache_note = (
                f"cache w{getattr(u, 'cache_creation_input_tokens', 0) or 0}"
                f"/r{getattr(u, 'cache_read_input_tokens', 0) or 0}")
            self.messages.append({"role": "assistant", "content": resp.content})
            text = " ".join(b.text for b in resp.content if b.type == "text")
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
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out)})
            self.messages.append({"role": "user", "content": results})


class OpenAIBackend:
    """Responses API: reasoning and tool calls coexist here, unlike the legacy
    chat-completions endpoint. State is server-side via previous_response_id,
    which also threads reasoning items across tool calls."""
    key = assistant.PROVIDER_KEY["openai"]

    def __init__(self, secrets, model, effort="low", voice=None):
        import openai
        self.client = openai.OpenAI(api_key=secrets[self.key])
        self.model = model
        self.effort = effort            # none|minimal|low|medium|high (model-dep)
        self.prev = None
        self.cache_note = ""
        self.messages = []              # trace mirror; real state is server-side
        self.server_tools = assistant.server_tools(voice, "openai") if voice else []

    def turn(self, system_text, user_text, impls):
        self.messages.append({"role": "user", "content": user_text})
        pending = [{"role": "user", "content": user_text}]
        while True:
            resp = self.client.responses.create(
                model=self.model, instructions=system_text, input=pending,
                tools=assistant.openai_tools(set(impls)) + self.server_tools,
                reasoning={"effort": self.effort},
                max_output_tokens=1500, previous_response_id=self.prev)
            self.prev = resp.id
            det = getattr(resp.usage, "input_tokens_details", None)
            self.cache_note = (
                f"cache r{getattr(det, 'cached_tokens', 0) or 0}" if det else "")
            # Server-side searches are ITEMS in resp.output, not function
            # calls: filtering to function_call drops them entirely and the
            # trace then carries no evidence a lookup happened.
            for o in resp.output:
                hit = llm_audit.search_item(o)
                if hit:
                    self.messages.append({"role": "tool", "name": hit[0],
                                          "query": hit[1],
                                          "status": getattr(o, "status", None)})
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                self.messages.append({"role": "assistant",
                                      "content": resp.output_text})
                return resp.output_text
            pending = []
            for c in calls:
                args = json.loads(c.arguments or "{}")
                out = impls[c.name](args)
                print(f"  [tool] {c.name}({args}) -> {out}")
                self.messages.append({"role": "tool", "name": c.name,
                                      "args": args, "out": out})
                pending.append({"type": "function_call_output",
                                "call_id": c.call_id, "output": json.dumps(out)})


BACKENDS = {"anthropic": AnthropicBackend, "openai": OpenAIBackend}


def repl(cfg, secrets, log, dry_run=True, provider=None, model=None, effort=None):
    """--text mode: type transcripts, see replies + tool calls + latency. The
    voice pipeline's system prompt and tool schemas, but only the base tool set
    - no operations, media or steam. Pick with --provider anthropic|openai
    [--model <id>] [--effort none|low|medium|high]."""
    from dispatch import Dispatch

    provider = provider or cfg["voice"]["assistantProvider"]
    if provider not in BACKENDS:
        print(f"unknown provider '{provider}' - one of {list(BACKENDS)}")
        return 2
    keyname = assistant.PROVIDER_KEY[provider]
    if not cglib.real_key(secrets.get(keyname)):
        print(f"{keyname} is a placeholder - add it to secrets.json for {provider}")
        return 1

    impls = assistant.tool_impls(Dispatch(cfg, log, dry_run=dry_run), log)
    effort = effort or cfg["voice"]["assistantReasoningEffort"]
    backend = BACKENDS[provider](secrets, model or assistant.default_model(cfg["voice"], provider),
                                 effort=effort, voice=cfg["voice"])
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
        traces.save(f"repl-{provider}", backend.messages,
                    {"model": backend.model, "dry_run": dry_run})
    return 0
