"""Pipecat's spans, in the shape Sentry's agent monitoring indexes.

Pipecat already emits the OTel gen_ai conventions - operation name, provider,
model, token usage, system instructions - but it names every LLM span "llm"
and never sets `sentry.op`, which is what Sentry keys the Agents dashboard and
Conversations off. Without this module those spans arrive as anonymous
children in a waterfall and the whole agent-monitoring product stays empty.

The rewrite happens at EXPORT, not on the span itself: pipecat sets `output`
and `metrics.ttfb` in a finally block, so there is nothing to read at start,
and a SpanProcessor.on_end gets a snapshot the SDK does not promise is
mutable. An exporter wrapper is also the only seam here with a public
contract - a SpanProcessor has private methods the SDK calls (1.44 added
`_on_ending`), so a duck-typed one breaks on an SDK bump and a real subclass
would force an OTel import at module scope, which sentry.setup's ImportError
path exists to avoid.

The correlation ids cannot come from events.current(): export runs on a batch
thread with none of the calling context. sentry.session_trace pins them here
for the length of a voice session instead - there is only ever one, and the
text lane sets its own at span creation.

tests/test_genai.py pins the attribute names on both sides. It is the only
thing that will notice a pipecat upgrade quietly emptying the dashboard.
"""

from __future__ import annotations

import json
from typing import Any

# Sentry derives a gen_ai span's op from `sentry.op`, which must read
# "gen_ai.{gen_ai.operation.name}", and it recognises only these four
# operations. Pipecat also stamps gen_ai.operation.name on its STT and TTS
# spans; those stay plain spans - visible in the waterfall, absent from the
# Agents dashboard - which is right, because they are not model calls Sentry
# can cost or replay.
CHAT_OPS = frozenset(("chat", "embeddings", "generate_content", "text_completion"))

# Pipecat's attribute -> Sentry's. Messages and tools are already JSON strings
# by the time pipecat sets them and Sentry accepts the legacy {role, content}
# message form, so these pass through verbatim instead of being rebuilt
# element by element.
PASSTHROUGH = (
    ("input", "gen_ai.input.messages"),
    ("tools", "gen_ai.tool.definitions"),
    ("metrics.ttfb", "gen_ai.response.time_to_first_chunk"),
)


# The ids the next spans are stamped with. A module slot and not a ContextVar:
# the reader is a batch export thread with none of the calling context.
#
# Two setters because the ids have different lifetimes: the session is pinned
# once by sentry.session_trace and lasts the whole conversation; the turn is
# minted per utterance, deep inside the pipeline, long after the session began.
_ids: dict = {}


def set_session(session=None) -> None:
    """Start a conversation, or end one. Clears the turn either way: a turn
    never outlives the session it belongs to."""
    _ids.clear()
    if session:
        # What collects a session's turns into one Sentry Conversation. It is
        # our `session`, so a Conversation and a run of JSONL lines are the
        # same thing seen twice.
        _ids["gen_ai.conversation.id"] = session


def set_turn(turn=None) -> None:
    """Point the next spans at one utterance. Joins them to that turn's log
    lines, and to the gaming PC's half of the same intent."""
    if turn:
        _ids["couch.turn"] = turn
    else:
        _ids.pop("couch.turn", None)


def sentry_attributes(attrs: dict) -> dict:
    """Attributes to ADD to a pipecat gen_ai span; {} when the span is not one
    Sentry indexes. Never overwrites an attribute the span already carries."""
    op = attrs.get("gen_ai.operation.name")
    if op not in CHAT_OPS:
        return {}
    add: dict[str, Any] = {"sentry.op": f"gen_ai.{op}"}
    for k, v in _ids.items():
        if k not in attrs:
            add[k] = v
    model = attrs.get("gen_ai.request.model")
    if model and "gen_ai.response.model" not in attrs:
        # Sentry requires it and pipecat never sets it. The requested model is
        # the honest answer - pipecat does not surface the concrete one.
        add["gen_ai.response.model"] = model
    for src, dst in PASSTHROUGH:
        if src in attrs and dst not in attrs:
            add[dst] = attrs[src]
    out = attrs.get("output")
    if out and "gen_ai.output.messages" not in attrs:
        add["gen_ai.output.messages"] = json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": str(out)}]}]
        )
    return add


def span_name(attrs: dict) -> str | None:
    """Sentry's convention, "chat <model>", or None to keep pipecat's name.
    Pipecat calls every LLM span "llm", which collapses every model into one
    row in the dashboard."""
    op = attrs.get("gen_ai.operation.name")
    model = attrs.get("gen_ai.request.model")
    return f"{op} {model}" if op in CHAT_OPS and model else None


def reshape(span):
    """A copy of `span` in Sentry's shape, or `span` itself when there is
    nothing to change. Never raises: a span this cannot reshape is still worth
    exporting, so a renamed pipecat attribute costs the dashboard and not the
    trace."""
    try:
        attrs = dict(span.attributes or {})
        add = sentry_attributes(attrs)
        if not add:
            return span
        from opentelemetry.sdk.trace import ReadableSpan

        return ReadableSpan(
            name=span_name(attrs) or span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=dict(attrs, **add),
            events=span.events,
            links=span.links,
            kind=span.kind,
            instrumentation_scope=span.instrumentation_scope,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
        )
    except Exception:
        return span


class SentryShape:
    """Wraps the OTLP exporter, reshaping gen_ai spans on the way out."""

    def __init__(self, inner):
        self.inner = inner

    def export(self, spans):
        return self.inner.export([reshape(s) for s in spans])

    def shutdown(self):
        return self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)
