"""Provider-executed tool calls (OpenAI web_search).

Pipecat (through 1.8.1) ignores server-side tool items, so llm_audit tees the
event stream. The tee sits in the path of every token of every conversation: it
must pass events through unchanged and in order, and never raise into the
pipeline. Fakes only - no network, no keys.
"""

import asyncio
import types

import pytest

from helpers import CapturingLog
from slopstation.agent.brain import llm_audit


def ev(kind, item=None):
    return types.SimpleNamespace(type=kind, item=item)


def search_item(query, kind="web_search_call", status="completed"):
    return types.SimpleNamespace(
        type=kind,
        status=status,
        action=types.SimpleNamespace(type="search", query=query),
    )


class FakeStream:
    def __init__(self, evs):
        self.evs, self.closes = evs, 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self.evs:
            yield e

    async def close(self):
        self.closes += 1


class FakeService:
    """The two pipecat internals llm_audit reaches for: the OpenAI client's
    responses.create, and _process_context."""

    def __init__(self, stream):
        async def create(**params):
            return stream

        responses = types.SimpleNamespace(create=create)
        self._client: types.SimpleNamespace = types.SimpleNamespace(responses=responses)

    async def _process_context(self, ctx):
        return "ok"


class FakeContext:
    def __init__(self):
        self.messages: list[dict] = []

    def add_message(self, m):
        self.messages.append(m)


class FakeTracing:
    def __init__(self):
        self.spans: list[tuple] = []

    def tool_span(self, kind, query, status=None):
        self.spans.append((kind, query, status))


async def _create_and_drain(svc, **params):
    stream = await svc._client.responses.create(**params)
    return [e async for e in stream.__aiter__()]


def drain(svc, **params):
    """One streaming create through the (audited) client, drained."""
    return asyncio.run(_create_and_drain(svc, **params))


@pytest.fixture
def turn():
    """One audited turn in which the model ran a search: the service, the
    log and span sink it was installed with, the context the note lands in,
    and what the tee yielded."""
    log = CapturingLog("audit")
    evs = [
        ev("response.output_text.delta"),
        ev("response.output_item.done", search_item("couch co-op 2026")),
        ev("response.output_text.delta"),
        ev("response.completed"),
    ]
    svc = FakeService(FakeStream(evs))
    spans = FakeTracing()
    ctx = FakeContext()
    live = llm_audit.install(svc, log, spans=spans, context=ctx)
    got = drain(svc, model="x", stream=True)
    return types.SimpleNamespace(
        log=log, evs=evs, svc=svc, spans=spans, ctx=ctx, live=live, got=got
    )


def test_the_tee_is_transparent(turn):
    assert turn.live is True
    assert turn.got == turn.evs, "the tee altered or reordered the event stream"


def test_the_search_is_seen(turn):
    rec = [r for r in turn.log.records if r["event"] == "web_search"]
    assert len(rec) == 1, rec
    assert rec[0]["query"] == "couch co-op 2026"
    assert rec[0]["kind"] == "web_search_call" and rec[0]["status"] == "completed"
    assert turn.spans.spans == [("web_search_call", "couch co-op 2026", "completed")]


def test_only_done_counts_and_only_search_items():
    # Both `added` and `done` carry the item; only `done` counts, and that
    # is where the query is final.
    svc = FakeService(
        FakeStream(
            [
                ev("response.output_item.added", search_item("q")),
                ev("response.output_item.done", search_item("q")),
                ev(
                    "response.output_item.done",
                    types.SimpleNamespace(type="function_call", name="launch_game"),
                ),
                ev("response.output_item.done", types.SimpleNamespace(type="message")),
            ]
        )
    )
    log = CapturingLog("audit")
    llm_audit.install(svc, log)
    drain(svc, stream=True)
    hits = [r for r in log.records if r["event"] == "web_search"]
    assert len(hits) == 1, f"expected one record, got {hits}"


def test_a_new_search_family_member_is_recorded():
    # A new search family member must be recorded, not silently dropped.
    svc = FakeService(
        FakeStream(
            [ev("response.output_item.done", search_item("f", kind="file_search_call"))]
        )
    )
    log = CapturingLog("audit")
    llm_audit.install(svc, log)
    drain(svc, stream=True)
    assert [r["kind"] for r in log.records if r["event"] == "web_search"] == [
        "file_search_call"
    ]


def test_the_model_is_told_on_the_next_turn(turn):
    # Mid-stream mutation corrupts a conversation, so the note waits.
    assert turn.ctx.messages == [], "context was mutated mid-turn"
    asyncio.run(turn.svc._process_context(turn.ctx))
    assert len(turn.ctx.messages) == 1, turn.ctx.messages
    note = turn.ctx.messages[0]
    assert note["role"] == "system" and "couch co-op 2026" in note["content"]
    assert "where that answer came from" in note["content"]
    asyncio.run(turn.svc._process_context(turn.ctx))
    assert len(turn.ctx.messages) == 1, "the note repeated on a later turn"


def test_a_throwing_span_sink_costs_no_event():
    # Nothing here may cost a conversation.
    boom = FakeService(FakeStream([ev("response.output_item.done", search_item("q"))]))

    class Angry:
        def tool_span(self, *a, **kw):
            raise RuntimeError("span sink is down")

    llm_audit.install(boom, CapturingLog("audit"), spans=Angry())
    out = drain(boom, stream=True)
    assert len(out) == 1, "a throwing sink swallowed an event"


def test_a_moved_pipecat_internal_disables_the_audit_not_the_voice():
    naked = types.SimpleNamespace(_client=types.SimpleNamespace())
    assert llm_audit.install(naked, CapturingLog("audit")) is False


def test_close_reaches_the_wrapped_stream():
    s = FakeStream([])
    svc = FakeService(s)
    llm_audit.install(svc, CapturingLog("audit"))
    tee = asyncio.run(svc._client.responses.create(stream=True))
    asyncio.run(tee.close())
    assert s.closes == 1, "close() did not reach the wrapped stream"


def test_a_non_streaming_create_passes_through_untouched():
    # Non-streaming create (pipecat's run_inference passes stream=False and
    # reads .output_text) must come back raw - the tee has no such surface.
    plain = types.SimpleNamespace(output_text="hi")
    svc = FakeService(plain)
    llm_audit.install(svc, CapturingLog("audit"))
    out = asyncio.run(svc._client.responses.create(stream=False))
    assert out is plain, "a non-streaming response must pass through untouched"


def test_installs_on_the_real_pipecat_class():
    # Fakes would survive a pipecat move of _client; check the real one.
    llm = pytest.importorskip("pipecat.services.openai.responses.llm")

    real = llm.OpenAIResponsesHttpLLMService(
        api_key="sk-not-used-no-call-is-made",
        settings=llm.OpenAIResponsesHttpLLMService.Settings(
            model="gpt-5.6-luna",
            system_instruction="x",
            max_completion_tokens=10,
            reasoning=llm.OpenAIResponsesReasoningConfig(effort="none"),
        ),
    )
    before = real._client.responses.create
    assert llm_audit.install(real, CapturingLog("audit")) is True, (
        "pipecat's client shape moved - production searches are invisible"
    )
    assert real._client.responses.create is not before
