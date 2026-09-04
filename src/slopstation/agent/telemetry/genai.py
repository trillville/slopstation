"""Adapt Pipecat's OpenTelemetry spans for Sentry agent monitoring.

Spans are reshaped during export, after Pipecat has added response data.
Session and turn IDs are stored here because export runs on another thread.
"""

from __future__ import annotations

import json
from typing import Any

# Only model operations belong in Sentry's agent dashboard.
CHAT_OPS = frozenset(("chat", "embeddings", "generate_content", "text_completion"))

# Map Pipecat attributes to their Sentry equivalents.
PASSTHROUGH = (
    ("input", "gen_ai.input.messages"),
    ("tools", "gen_ai.tool.definitions"),
    ("metrics.ttfb", "gen_ai.response.time_to_first_chunk"),
)


# The batch export thread cannot read the caller's ContextVars.
_ids: dict = {}


def set_session(session=None) -> None:
    """Set or clear the active conversation ID."""
    _ids.clear()
    if session:
        _ids["gen_ai.conversation.id"] = session


def set_turn(turn=None) -> None:
    """Set or clear the active utterance ID."""
    if turn:
        _ids["couch.turn"] = turn
    else:
        _ids.pop("couch.turn", None)


def sentry_attributes(attrs: dict) -> dict:
    """Return missing Sentry attributes for a model span."""
    op = attrs.get("gen_ai.operation.name")
    if op not in CHAT_OPS:
        return {}
    add: dict[str, Any] = {"sentry.op": f"gen_ai.{op}"}
    for k, v in _ids.items():
        if k not in attrs:
            add[k] = v
    model = attrs.get("gen_ai.request.model")
    if model and "gen_ai.response.model" not in attrs:
        # Pipecat does not expose a separate response model.
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
    """Return Sentry's model-specific span name, if applicable."""
    op = attrs.get("gen_ai.operation.name")
    model = attrs.get("gen_ai.request.model")
    return f"{op} {model}" if op in CHAT_OPS and model else None


def reshape(span):
    """Return a copy of a span with Sentry attributes, when applicable."""
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
