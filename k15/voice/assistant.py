"""Assistant lane: the catalog-in-context brain.

One source of truth for the system prompt, the tool schemas, and the tool
implementations - the Pipecat pipeline (voice) and the --text REPL (bench +
model A/B instrument) both consume them, and every tool routes through the
same dispatch.py as Tier 1. Client-side strictness: an appid that isn't in
the index is refused at the tool boundary, whatever the model dreamt up.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import cglib                                    # noqa: E402
import library                                  # noqa: E402

RULES = (
    "You are the voice assistant for a couch gaming setup (Steam on a TV). "
    "Answers are SPOKEN aloud: plain text only, no markdown, no emoji, at "
    "most two short sentences unless asked for detail. For list questions "
    "lead with the count, name at most three (installed first, then most "
    "played), and offer the rest. Only discuss games from the catalog below. "
    "Use tools for every action; appids come only from the catalog. If a "
    "request is ambiguous, ask one short clarifying question. If something "
    "fails, say so plainly.\n\n"
    "CATALOG (appid|name|tags|genres|hours|lastPlayed|installed|controller):\n"
)


def system_instruction():
    return RULES + "\n".join(library.catalog_lines())


def known_appids():
    index = library.load()
    ids = {r["appid"] for r in index.get("installed", [])}
    ids.update(int(a) for a in index.get("owned", {}))
    return ids


def installed_name(appid):
    for r in library.load().get("installed", []):
        if r["appid"] == appid:
            return r["name"]
    return None


def tool_impls(dispatch, log):
    """name -> fn(args: dict) -> dict. Shared by pipeline and REPL."""
    def launch_game(args):
        appid = int(args.get("appid", 0))
        if appid not in known_appids():
            log(f"tool launch_game REFUSED unknown appid {appid}")
            return {"ok": False, "error": f"appid {appid} is not in the catalog"}
        if installed_name(appid) is None:
            return {"ok": False, "error": "that game is owned but not "
                    "installed - installing needs the controller"}
        r = dispatch.play_game(appid)
        return {"ok": r.ok, "detail": r.detail}

    plain = {"end_session": dispatch.end_session,
             "start_session": dispatch.start_session,
             "volume_up": dispatch.volume_up,
             "volume_down": dispatch.volume_down,
             "mute": dispatch.mute_toggle}

    def control(args):
        action = args.get("action")
        if action == "set_volume":
            if "level" not in args:
                return {"ok": False, "error": "set_volume needs level 0-100"}
            r = dispatch.volume_set(int(args["level"]))
        elif action == "switch_input":
            r = dispatch.switch_input(str(args.get("input", "")))
        elif action in plain:
            r = plain[action]()
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        return {"ok": r.ok, "detail": r.detail}

    def get_now_playing(args):
        r = dispatch.now_playing()
        if not r.ok:
            return {"ok": False, "error": r.detail}
        appid = int(r.detail) if str(r.detail).isdigit() else 0
        return {"ok": True, "appid": appid,
                "name": installed_name(appid) if appid else None}

    def get_game_details(args):
        appid = int(args.get("appid", 0))
        meta = library.load_meta().get(str(appid))
        name = installed_name(appid)
        if not (meta or name):
            return {"ok": False, "error": "unknown appid"}
        return {"ok": True, "name": name, **(meta or {})}

    return {"launch_game": launch_game, "control": control,
            "get_now_playing": get_now_playing,
            "get_game_details": get_game_details}


TOOL_DEFS = [
    ("launch_game", "Launch a game from the catalog by appid.",
     {"appid": {"type": "integer", "description": "appid from the catalog"}},
     ["appid"]),
    ("control", "Control the system: end_session, start_session, volume_up, "
     "volume_down, mute, set_volume (with level 0-100), switch_input "
     "(with input name).",
     {"action": {"type": "string",
                 "enum": ["end_session", "start_session", "volume_up",
                          "volume_down", "mute", "set_volume", "switch_input"]},
      "level": {"type": "integer"}, "input": {"type": "string"}},
     ["action"]),
    ("get_now_playing", "What game is currently running, if any.", {}, []),
    ("get_game_details", "Details (tags, description, score) for one appid.",
     {"appid": {"type": "integer"}}, ["appid"]),
]


def function_schemas(impls):
    """Pipecat FunctionSchema list with auto-registering async handlers.
    Tool impls call blocking dispatch (ssh/serial) - run them off the event
    loop so audio and the Flux socket keep flowing during a tool call."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema

    def wrap(fn):
        async def handler(params):
            out = await asyncio.to_thread(fn, dict(params.arguments))
            await params.result_callback(out)
        return handler

    return [FunctionSchema(name=n, description=d, properties=p, required=r,
                           handler=wrap(impls[n]))
            for n, d, p, r in TOOL_DEFS]


def anthropic_tools():
    return [{"name": n, "description": d,
             "input_schema": {"type": "object", "properties": p, "required": r}}
            for n, d, p, r in TOOL_DEFS]


def openai_tools():
    # Responses API tool shape is FLAT (name/parameters at top level) - the
    # nested {"function": {...}} form is chat-completions only.
    return [{"type": "function", "name": n, "description": d,
             "parameters": {"type": "object", "properties": p, "required": r}}
            for n, d, p, r in TOOL_DEFS]


# --- provider backends: one turn (with its tool loop) -> reply text -----------
# System prompt, tool schemas, and tool impls are provider-neutral; only the
# request/response shape differs. Each backend holds its own conversation state.

class AnthropicBackend:
    key = "anthropicApiKey"

    def __init__(self, secrets, model, effort=None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=secrets[self.key])
        self.model = model
        self.messages = []

    def turn(self, system_text, user_text, impls):
        system = [{"type": "text", "text": system_text,
                   "cache_control": {"type": "ephemeral"}}]
        self.messages.append({"role": "user", "content": user_text})
        while True:
            resp = self.client.messages.create(
                model=self.model, max_tokens=400, system=system,
                messages=self.messages, tools=anthropic_tools())
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                return " ".join(b.text for b in resp.content if b.type == "text")
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
    key = "openaiApiKey"

    def __init__(self, secrets, model, effort="low"):
        import openai
        self.client = openai.OpenAI(api_key=secrets[self.key])
        self.model = model
        self.effort = effort            # none|minimal|low|medium|high (model-dep)
        self.prev = None

    def turn(self, system_text, user_text, impls):
        pending = [{"role": "user", "content": user_text}]
        while True:
            resp = self.client.responses.create(
                model=self.model, instructions=system_text, input=pending,
                tools=openai_tools(), reasoning={"effort": self.effort},
                max_output_tokens=1500, previous_response_id=self.prev)
            self.prev = resp.id
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                return resp.output_text
            pending = []
            for c in calls:
                args = json.loads(c.arguments or "{}")
                out = impls[c.name](args)
                print(f"  [tool] {c.name}({args}) -> {out}")
                pending.append({"type": "function_call_output",
                                "call_id": c.call_id, "output": json.dumps(out)})


BACKENDS = {"anthropic": AnthropicBackend, "openai": OpenAIBackend}


def default_model(cfg, provider):
    return (cfg["voice"].get("assistantModelOpenai", "gpt-5.6-luna")
            if provider == "openai" else cfg["voice"]["assistantModel"])


def repl(cfg, secrets, log, dry_run=True, provider=None, model=None, effort=None):
    """--text mode: type transcripts, see replies + tool calls + latency. The
    20-query canned set and the model A/B run through this - same system prompt,
    tool schemas, and impls as the voice pipeline, either provider. Pick with
    --provider anthropic|openai [--model <id>] [--effort none|low|medium|high]."""
    from dispatch import Dispatch

    provider = provider or cfg["voice"].get("assistantProvider", "anthropic")
    if provider not in BACKENDS:
        print(f"unknown provider '{provider}' - one of {list(BACKENDS)}")
        return 2
    keyname = BACKENDS[provider].key
    if not cglib.real_key(secrets.get(keyname)):
        print(f"{keyname} is a placeholder - add it to secrets.json for {provider}")
        return 1

    impls = tool_impls(Dispatch(cfg, log, dry_run=dry_run), log)
    effort = effort or cfg["voice"].get("assistantReasoningEffort", "low")
    backend = BACKENDS[provider](secrets, model or default_model(cfg, provider),
                                 effort=effort)
    system_text = system_instruction()
    tag = f"{provider}/{backend.model}"
    if provider == "openai":
        tag += f" effort={effort}"
    print(f"assistant REPL - {tag}, dry_run={dry_run}. Empty line to quit.")
    while True:
        try:
            q = input("you> ").strip()
        except EOFError:
            break
        if not q:
            break
        t0 = time.time()
        text = backend.turn(system_text, q, impls)
        print(f"assistant ({time.time() - t0:.1f}s)> {text}")
    return 0
