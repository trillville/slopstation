"""Blind test: provider-executed tool calls (OpenAI web_search). Run:
    .venv\\Scripts\\python tests\\test_llm_audit.py

Pipecat (through 1.8.1) ignores server-side tool items, so llm_audit tees the
event stream. The tee sits in the path of every token of every conversation: it
must pass events through unchanged and in order, and never raise into the
pipeline. Fakes only - no network, no keys.
"""
import asyncio
import types

import _bootstrap  # noqa: F401

import cglib
from agent.brain import llm_audit


def ev(kind, item=None):
    return types.SimpleNamespace(type=kind, item=item)


def search_item(query, kind="web_search_call", status="completed"):
    return types.SimpleNamespace(
        type=kind, status=status,
        action=types.SimpleNamespace(type="search", query=query))


class FakeStream:
    def __init__(self, evs):
        self.evs, self.closed = evs, False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self.evs:
            yield e

    async def close(self):
        self.closed = True


class FakeService:
    def __init__(self, stream):
        self.stream = stream
        self.calls = 0
        responses = types.SimpleNamespace(create=self._create)
        self._client = types.SimpleNamespace(responses=responses)
        self.contexts = []

    async def _create(self, **params):
        self.calls += 1
        return self.stream

    async def _process_context(self, ctx):
        self.contexts.append(list(ctx.messages))
        return "ok"


class FakeContext:
    def __init__(self):
        self.messages = []

    def add_message(self, m):
        self.messages.append(m)


class FakeTracing:
    def __init__(self):
        self.spans = []

    def tool_span(self, kind, query, status=None):
        self.spans.append((kind, query, status))


async def drain(stream):
    return [e async for e in stream.__aiter__()]


def main():
    log = cglib.CapturingLog("audit")

    # -- the tee is transparent -----------------------------------------------
    evs = [ev("response.output_text.delta"),
           ev("response.output_item.done", search_item("couch co-op 2026")),
           ev("response.output_text.delta"),
           ev("response.completed")]
    svc = FakeService(FakeStream(evs))
    tracing = FakeTracing()
    ctx = FakeContext()
    assert llm_audit.install(svc, log, tracing=tracing, context=ctx) is True

    stream = asyncio.run(svc._client.responses.create(model="x", stream=True))
    got = asyncio.run(drain(stream))
    assert got == evs, "the tee altered or reordered the event stream"
    print("  tee: every event passes through unchanged and in order")

    # -- the search is seen ---------------------------------------------------
    rec = [r for r in log.records if r["event"] == "web_search"]
    assert len(rec) == 1, rec
    assert rec[0]["query"] == "couch co-op 2026"
    assert rec[0]["kind"] == "web_search_call" and rec[0]["status"] == "completed"
    assert tracing.spans == [("web_search_call", "couch co-op 2026", "completed")]
    print("  search: logged once and spanned, with its query")

    # Both `added` and `done` carry the item; only `done` counts, and that
    # is where the query is final.
    svc2 = FakeService(FakeStream([
        ev("response.output_item.added", search_item("q")),
        ev("response.output_item.done", search_item("q")),
        ev("response.output_item.done",
           types.SimpleNamespace(type="function_call", name="launch_game")),
        ev("response.output_item.done", types.SimpleNamespace(type="message")),
    ]))
    log2 = cglib.CapturingLog("audit")
    llm_audit.install(svc2, log2)
    asyncio.run(drain(asyncio.run(svc2._client.responses.create(stream=True))))
    hits = [r for r in log2.records if r["event"] == "web_search"]
    assert len(hits) == 1, f"expected one record, got {hits}"
    print("  search: counted once (not on `added`), function_call ignored")

    # A new search family member must be recorded, not silently dropped.
    svc3 = FakeService(FakeStream([
        ev("response.output_item.done", search_item("f", kind="file_search_call"))]))
    log3 = cglib.CapturingLog("audit")
    llm_audit.install(svc3, log3)
    asyncio.run(drain(asyncio.run(svc3._client.responses.create(stream=True))))
    assert [r["kind"] for r in log3.records if r["event"] == "web_search"] \
        == ["file_search_call"]
    print("  search: matches the family, not one literal type")

    # -- the model gets told, on the NEXT turn --------------------------------
    # Mid-stream mutation corrupts a conversation, so the note waits.
    assert ctx.messages == [], "context was mutated mid-turn"
    asyncio.run(svc._process_context(ctx))
    assert len(ctx.messages) == 1, ctx.messages
    note = ctx.messages[0]
    assert note["role"] == "system" and "couch co-op 2026" in note["content"]
    assert "where that answer came from" in note["content"]
    asyncio.run(svc._process_context(ctx))
    assert len(ctx.messages) == 1, "the note repeated on a later turn"
    print("  context: the model is told what it searched, once, a turn later")

    # -- nothing here may cost a conversation ---------------------------------
    boom = FakeService(FakeStream([ev("response.output_item.done",
                                      search_item("q"))]))

    class Angry:
        def tool_span(self, *a, **kw):
            raise RuntimeError("tracing is down")

    llm_audit.install(boom, log, tracing=Angry())
    out = asyncio.run(drain(asyncio.run(
        boom._client.responses.create(stream=True))))
    assert len(out) == 1, "a throwing sink swallowed an event"

    # A pipecat internal that moved must disable the audit, not the voice.
    naked = types.SimpleNamespace(_client=types.SimpleNamespace())
    assert llm_audit.install(naked, log) is False
    print("  fail-soft: a throwing sink and a moved client both shrug")

    s = FakeStream([])
    svc4 = FakeService(s)
    llm_audit.install(svc4, log)
    tee = asyncio.run(svc4._client.responses.create(stream=True))
    asyncio.run(tee.close())
    assert s.closed, "close() did not reach the wrapped stream"
    print("  close: forwarded, so no socket outlives the turn")

    # Non-streaming create (pipecat's run_inference passes stream=False and
    # reads .output_text) must come back raw - the tee has no such surface.
    plain = types.SimpleNamespace(output_text="hi")
    svc5 = FakeService(plain)
    llm_audit.install(svc5, log)
    out = asyncio.run(svc5._client.responses.create(stream=False))
    assert out is plain, "a non-streaming response must pass through untouched"
    print("  non-stream: create(stream=False) returns the raw response")

    # -- against the REAL pipecat class ---------------------------------------
    # Fakes would survive a pipecat move of _client; check the real one.
    try:
        from pipecat.services.openai.responses.llm import (
            OpenAIResponsesHttpLLMService, OpenAIResponsesReasoningConfig)
    except ImportError:
        print("  real class: SKIPPED - pipecat not installed in this venv")
    else:
        real = OpenAIResponsesHttpLLMService(
            api_key="sk-not-used-no-call-is-made",
            settings=OpenAIResponsesHttpLLMService.Settings(
                model="gpt-5.6-luna", system_instruction="x",
                max_completion_tokens=10,
                reasoning=OpenAIResponsesReasoningConfig(effort="none")))
        before = real._client.responses.create
        assert llm_audit.install(real, log) is True, \
            "pipecat's client shape moved - production searches are invisible"
        assert real._client.responses.create is not before
        print("  real class: install() binds to pipecat "
              f"{OpenAIResponsesHttpLLMService.__module__.split('.')[0]}'s "
              "actual client")

    print("OK - llm_audit: transparent tee, search recorded, context fed, "
          "fail-soft")


if __name__ == "__main__":
    main()
