"""C3 assistant lane: the catalog-in-context brain.

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


def repl(cfg, secrets, log, dry_run=True, model=None):
    """--text mode: type transcripts, see replies + tool calls. The 20-query
    canned set and the model A/B run through this. Raw Anthropic SDK with the
    SAME system prompt + tool impls as the voice pipeline."""
    import anthropic
    from dispatch import Dispatch

    impls = tool_impls(Dispatch(cfg, log, dry_run=dry_run), log)
    client = anthropic.Anthropic(api_key=secrets["anthropicApiKey"])
    model = model or cfg["voice"]["assistantModel"]
    system = [{"type": "text", "text": system_instruction(),
               "cache_control": {"type": "ephemeral"}}]
    messages = []
    print(f"assistant REPL - model {model}, dry_run={dry_run}. "
          f"Empty line to quit.")
    while True:
        try:
            q = input("you> ").strip()
        except EOFError:
            break
        if not q:
            break
        messages.append({"role": "user", "content": q})
        t0 = time.time()
        while True:
            resp = client.messages.create(model=model, max_tokens=400,
                                          system=system, messages=messages,
                                          tools=anthropic_tools())
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = impls[block.name](dict(block.input))
                    print(f"  [tool] {block.name}({dict(block.input)}) -> {out}")
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps(out)})
            messages.append({"role": "user", "content": results})
        text = " ".join(b.text for b in resp.content if b.type == "text")
        print(f"assistant ({time.time() - t0:.1f}s)> {text}")
    return 0
