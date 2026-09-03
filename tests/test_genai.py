"""The pipecat -> Sentry span adapter.

This is the only thing that will notice a pipecat upgrade quietly emptying the
Agents dashboard, so it pins the attribute names on BOTH sides: the ones
pipecat writes (service_attributes.add_llm_span_attributes) and the ones
Sentry indexes. A failure here means one of the two moved.
"""

import json

from slopstation import events
from slopstation.agent.telemetry import genai

# Exactly what pipecat's add_llm_span_attributes + the decorator's finally
# block put on an LLM span. Copied from the shape, not invented.
PIPECAT_LLM = {
    "gen_ai.provider.name": "anthropic",
    "gen_ai.request.model": "claude-haiku-4-5",
    "gen_ai.operation.name": "chat",
    "gen_ai.output.type": "text",
    "gen_ai.system_instructions": "you are the couch",
    "stream": True,
    "input": '[{"role": "user", "content": "play hades"}]',
    "output": "launching Hades",
    "tools": '[{"name": "launch_game"}]',
    "tool_count": 1,
    "metrics.ttfb": 0.42,
    "gen_ai.usage.input_tokens": 900,
    "gen_ai.usage.output_tokens": 20,
}


def test_genai():

    # -- what Sentry needs added ----------------------------------------------
    add = genai.sentry_attributes(PIPECAT_LLM)
    # THE attribute: without it the span is an anonymous child and the Agents
    # dashboard never sees it.
    assert add["sentry.op"] == "gen_ai.chat", add
    # Required by Sentry, never set by pipecat.
    assert add["gen_ai.response.model"] == "claude-haiku-4-5"
    assert add["gen_ai.input.messages"] == PIPECAT_LLM["input"]
    assert add["gen_ai.tool.definitions"] == PIPECAT_LLM["tools"]
    assert add["gen_ai.response.time_to_first_chunk"] == 0.42
    out = json.loads(add["gen_ai.output.messages"])
    assert out[0]["role"] == "assistant"
    assert out[0]["parts"][0]["content"] == "launching Hades"

    # Token usage is pipecat's already and must NOT be rewritten - Sentry reads
    # the same names.
    assert not any(k.startswith("gen_ai.usage.") for k in add), add

    # -- spans Sentry does not index ------------------------------------------
    # Pipecat stamps gen_ai.operation.name on STT and TTS too. Those stay plain
    # spans: they are not model calls Sentry can cost, and a bogus sentry.op
    # would put them in the dashboard as chats.
    for op in ("stt", "tts", "setup", None):
        assert (
            genai.sentry_attributes(dict(PIPECAT_LLM, **{"gen_ai.operation.name": op}))
            == {}
        ), op
    assert genai.sentry_attributes({}) == {}

    # -- never clobbers --------------------------------------------------------
    already = dict(
        PIPECAT_LLM,
        **{
            "gen_ai.response.model": "claude-haiku-4-5-20251001",
            "gen_ai.input.messages": '[{"role":"user","content":"real"}]',
        },
    )
    add2 = genai.sentry_attributes(already)
    assert "gen_ai.response.model" not in add2
    assert "gen_ai.input.messages" not in add2

    # -- the span name ---------------------------------------------------------
    # Pipecat names every LLM span "llm", which collapses every model into one
    # dashboard row.
    assert genai.span_name(PIPECAT_LLM) == "chat claude-haiku-4-5"
    assert genai.span_name({"gen_ai.operation.name": "stt"}) is None
    assert genai.span_name({"gen_ai.operation.name": "chat"}) is None

    # -- the conversation stamp ------------------------------------------------
    # Pinned rather than read from events.current(): the reader is a batch
    # export thread with none of the calling context.
    genai.set_session("3b7e")
    genai.set_turn("9f2c1a")
    add3 = genai.sentry_attributes(PIPECAT_LLM)
    # One voice session is one Conversation is one `session` in the JSONL.
    assert add3["gen_ai.conversation.id"] == "3b7e", add3
    assert add3["couch.turn"] == "9f2c1a"

    # TWO setters, because the ids have different lifetimes: the session is
    # pinned once at session start, the turn is minted per utterance long
    # after. Setting a new turn must not disturb the session.
    genai.set_turn("aa11bb")
    add4 = genai.sentry_attributes(PIPECAT_LLM)
    assert add4["couch.turn"] == "aa11bb"
    assert add4["gen_ai.conversation.id"] == "3b7e", "a new turn dropped the session"

    # A turn never outlives its session.
    genai.set_session()
    cleared = genai.sentry_attributes(PIPECAT_LLM)
    assert "gen_ai.conversation.id" not in cleared
    assert "couch.turn" not in cleared, "the turn survived the session ending"

    # A span that already carries one keeps it - the text lane sets its own.
    genai.set_session("3b7e")
    assert "gen_ai.conversation.id" not in genai.sentry_attributes(
        dict(PIPECAT_LLM, **{"gen_ai.conversation.id": "own"})
    )
    genai.set_session()

    # -- reshape, against a real ReadableSpan ---------------------------------
    try:
        from opentelemetry.sdk.trace import ReadableSpan
    except ImportError:
        print("  reshape: SKIPPED - opentelemetry not installed in this venv")
    else:
        span = ReadableSpan(name="llm", attributes=dict(PIPECAT_LLM))
        new = genai.reshape(span)
        assert new is not span, "a chat span is rewritten"
        assert new.name == "chat claude-haiku-4-5", new.name
        assert new.attributes["sentry.op"] == "gen_ai.chat"
        # Everything pipecat set survives the copy.
        for k, v in PIPECAT_LLM.items():
            assert new.attributes[k] == v, k
        # A span with nothing to add comes back untouched, by identity.
        plain = ReadableSpan(name="tts", attributes={"gen_ai.operation.name": "tts"})
        assert genai.reshape(plain) is plain

    # A span whose attributes explode is exported unchanged rather than lost.
    class Broken:
        @property
        def attributes(self):
            raise RuntimeError("gone")

    b = Broken()
    assert genai.reshape(b) is b

    # -- the exporter wrapper --------------------------------------------------
    class Inner:
        def __init__(self):
            self.got = None
            self.flushed = False

        def export(self, spans):
            self.got = spans
            return "exported"

        def shutdown(self):
            return "down"

        def force_flush(self, timeout_millis=30000):
            self.flushed = True
            return True

    inner = Inner()
    wrap = genai.SentryShape(inner)
    assert wrap.export([b]) == "exported"
    assert inner.got == [b]
    assert wrap.shutdown() == "down"
    assert wrap.force_flush() is True and inner.flushed

    # -- end to end, through a real provider ----------------------------------
    # The seam that actually broke once: opentelemetry-sdk 1.44 calls private
    # methods on every registered SpanProcessor, so a duck-typed processor
    # blew up at span end. Nothing but wiring a real provider catches that.
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:
        print("  wiring: SKIPPED - opentelemetry not installed in this venv")
    else:
        from slopstation.agent.telemetry import sentry

        mem = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(genai.SentryShape(mem)))
        trace.set_tracer_provider(provider)
        sentry._on = True
        try:
            tok = events.context(session="3b7e", turn="9f2c1a")
            with sentry.session_trace() as trace_id:
                assert events.current()["trace"] == trace_id
                # As grammar_gate does when an utterance arrives.
                sentry.set_turn("9f2c1a")
                with trace.get_tracer("pipecat").start_as_current_span("llm") as s:
                    for k, v in PIPECAT_LLM.items():
                        s.set_attribute(k, v)
                sentry.tool_span("web_search", "hades reviews", "ok")
            # The pin does not outlive the session, or the next one's spans
            # would join a Conversation they were not part of.
            assert "trace" not in events.current()
            for gone in ("gen_ai.conversation.id", "couch.turn"):
                assert genai.sentry_attributes(PIPECAT_LLM).get(gone) is None, gone
            events.reset(tok)
        finally:
            sentry._on = False

        out = {s.name: s for s in mem.get_finished_spans()}
        assert set(out) == {"chat claude-haiku-4-5", "execute_tool web_search"}, out
        llm = out["chat claude-haiku-4-5"].attributes
        assert llm["sentry.op"] == "gen_ai.chat"
        assert llm["gen_ai.conversation.id"] == "3b7e", llm
        assert llm["couch.turn"] == "9f2c1a"
        assert llm["gen_ai.input.messages"] == PIPECAT_LLM["input"]
        tool = out["execute_tool web_search"].attributes
        assert tool["sentry.op"] == "gen_ai.execute_tool"
        assert tool["gen_ai.tool.call.result"] == "ok"
        # One session, one trace: the log line's `trace` field and every span
        # in the session carry the same id, which is the click from one to
        # the other.
        ids = {f"{s.context.trace_id:032x}" for s in mem.get_finished_spans()}
        assert ids == {trace_id}, (ids, trace_id)
