"""The assistant's --text REPL, and the two API clients it drives.

Typed transcripts in; replies, tool calls and latency out. This is the bench
and model-A/B instrument (voice-testing.md § 10): the SAME system prompt, tool
schemas and tool impls as the voice pipeline, against either provider's API
directly, with a fresh backend per conversation. Production never comes here
- the voice lane's LLM is pipecat's service (session_runtime._make_llm) -
which is why these clients left assistant.py: that file is the one source of
truth for what the model is told and can do, and it was carrying two
bench-only HTTP clients with their own conversation state beside it. Each
backend holds its own messages; bench/harness.py builds one per trial from
BACKENDS, and voice_agent --text calls repl().
"""
import json
import time

import cglib
import traces
from assistant import (PROVIDER_KEY, anthropic_tools, default_model,
                       openai_tools, server_tools, system_instruction,
                       tool_impls)


# --- provider backends: one turn (with its tool loop) -> reply text -----------
# System prompt, tool schemas, and tool impls are provider-neutral; only the
# request/response shape differs. Each backend holds its own conversation state.


class AnthropicBackend:
    key = PROVIDER_KEY["anthropic"]

    def __init__(self, secrets, model, effort=None, voice=None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=secrets[self.key])
        self.model = model
        self.messages = []
        self.cache_note = ""
        self.server_tools = server_tools(voice, "anthropic") if voice else []

    def turn(self, system_text, user_text, impls):
        # cache_control on the system block caches tools+system together
        # (render order is tools -> system -> messages; a breakpoint covers
        # everything before it). CAVEAT: small models have a minimum cacheable
        # prefix (4096 tokens on Haiku 4.5) below which the marker silently
        # does nothing - the REPL prints cache w/r so w0/r0 is visible.
        system = [{"type": "text", "text": system_text,
                   "cache_control": {"type": "ephemeral"}}]
        self.messages.append({"role": "user", "content": user_text})
        spoken = []          # text carried across pause_turn continuations
        while True:
            resp = self.client.messages.create(
                model=self.model, max_tokens=400, system=system,
                messages=self.messages,
                tools=anthropic_tools(set(impls)) + self.server_tools)
            u = resp.usage
            self.cache_note = (
                f"cache w{getattr(u, 'cache_creation_input_tokens', 0) or 0}"
                f"/r{getattr(u, 'cache_read_input_tokens', 0) or 0}")
            self.messages.append({"role": "assistant", "content": resp.content})
            text = " ".join(b.text for b in resp.content if b.type == "text")
            if resp.stop_reason == "pause_turn":
                # A long server-side search paused the turn mid-flight; the
                # documented contract is to re-send the partial assistant
                # content as-is and let the model continue. Server tool
                # blocks need no client-side result - only the accumulated
                # text matters to the caller.
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
    """Responses API - the interface OpenAI recommends for reasoning models;
    reasoning and tool calls coexist here (they don't cleanly on the legacy
    chat-completions endpoint). State is server-side via previous_response_id,
    which also threads reasoning items across tool calls for us."""
    key = PROVIDER_KEY["openai"]

    def __init__(self, secrets, model, effort="low", voice=None):
        import openai
        self.client = openai.OpenAI(api_key=secrets[self.key])
        self.model = model
        self.effort = effort            # none|minimal|low|medium|high (model-dep)
        self.prev = None
        self.cache_note = ""
        self.messages = []              # trace mirror; real state is server-side
        self.server_tools = server_tools(voice, "openai") if voice else []

    def turn(self, system_text, user_text, impls):
        self.messages.append({"role": "user", "content": user_text})
        pending = [{"role": "user", "content": user_text}]
        while True:
            resp = self.client.responses.create(
                model=self.model, instructions=system_text, input=pending,
                tools=openai_tools(set(impls)) + self.server_tools,
                reasoning={"effort": self.effort},
                max_output_tokens=1500, previous_response_id=self.prev)
            self.prev = resp.id
            det = getattr(resp.usage, "input_tokens_details", None)
            self.cache_note = (
                f"cache r{getattr(det, 'cached_tokens', 0) or 0}" if det else "")
            # Server-side searches are ITEMS in resp.output, not function
            # calls: filtering to function_call drops them entirely and the
            # trace then carries no evidence a lookup happened. That is not
            # cosmetic - a model's own account of itself is not evidence,
            # since it cannot tell afterwards whether a server-side tool ran.
            for o in resp.output:
                if "search" in (getattr(o, "type", "") or ""):
                    action = getattr(o, "action", None)
                    self.messages.append({
                        "role": "tool", "name": getattr(o, "type", "search"),
                        "query": getattr(action, "query", None) or str(action or "")[:200],
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
    20-query canned set and the model A/B run through this - same system prompt,
    tool schemas, and impls as the voice pipeline, either provider. Pick with
    --provider anthropic|openai [--model <id>] [--effort none|low|medium|high]."""
    from dispatch import Dispatch

    provider = provider or cfg["voice"]["assistantProvider"]
    if provider not in BACKENDS:
        print(f"unknown provider '{provider}' - one of {list(BACKENDS)}")
        return 2
    keyname = BACKENDS[provider].key
    if not cglib.real_key(secrets.get(keyname)):
        print(f"{keyname} is a placeholder - add it to secrets.json for {provider}")
        return 1

    impls = tool_impls(Dispatch(cfg, log, dry_run=dry_run), log)
    effort = effort or cfg["voice"]["assistantReasoningEffort"]
    backend = BACKENDS[provider](secrets, model or default_model(cfg, provider),
                                 effort=effort, voice=cfg["voice"])
    system_text = system_instruction(cfg)
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
                # transient API error shouldn't kill the bench session - the
                # error text is the answer to the probe.
                print(f"API error ({time.time() - t0:.1f}s)> {e}")
                continue
            note = f", {backend.cache_note}" if backend.cache_note else ""
            print(f"assistant ({time.time() - t0:.1f}s{note})> {text}")
    finally:
        # Ctrl-C included: a bench conversation worth having is worth keeping.
        traces.save(f"repl-{provider}", backend.messages,
                    {"model": backend.model, "dry_run": dry_run})
    return 0
