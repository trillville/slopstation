"""Send errors and voice-session traces to Sentry."""

import contextlib
import functools
import json
import logging
import random
from typing import cast

from slopstation import config, events
from slopstation.agent.telemetry import genai

SERVICE_NAME = "slopstation-voice"

_on = False


def is_on():
    """Return whether tracing is configured."""
    return _on


def otlp_target(dsn):
    """Build Sentry's OTLP trace endpoint and authentication headers."""
    from sentry_sdk.consts import VERSION, EndpointType
    from sentry_sdk.utils import Dsn

    auth = Dsn(dsn).to_auth(f"sentry.python/{VERSION}")
    return (
        auth.get_api_url(EndpointType.OTLP_TRACES),
        {"X-Sentry-Auth": auth.to_header()},
    )


def _init(dsn, log):
    """Initialize Sentry error reporting and trace linking."""
    try:
        import sentry_sdk
        from sentry_sdk.integrations.otlp import OTLPIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=events.ENV,
            integrations=[OTLPIntegration(setup_otlp_traces_exporter=False)],
            send_default_pii=True,
            enable_logs=False,
        )
        sentry_sdk.set_user({"id": events.HOST})
        log("lane_up", what="sentry", kind="errors")
        return True
    except ImportError as e:
        log.warn(
            "lane_disabled",
            what="sentry",
            reason="sentry-sdk not installed - rebuild the venv",
            err=str(e),
        )
        return False
    except Exception as e:
        log.error("sentry_setup_failed", err=repr(e))
        return False


def _trace(dsn, log):
    """Add the Sentry exporter to Pipecat's tracer provider."""
    global _on
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
        from pipecat.utils.tracing.setup import setup_tracing

        logging.getLogger("opentelemetry").setLevel(logging.WARNING)

        if not setup_tracing(service_name=SERVICE_NAME, console_export=False):
            log.warn(
                "lane_disabled",
                what="tracing",
                reason="pipecat setup_tracing returned false",
            )
            return False
        url, headers = otlp_target(dsn)
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            log.warn("lane_disabled", what="tracing", reason="no SDK tracer provider")
            return False
        exporter = genai.SentryShape(OTLPSpanExporter(endpoint=url, headers=headers))
        provider.add_span_processor(BatchSpanProcessor(cast(SpanExporter, exporter)))
        _on = True
        log("lane_up", what="tracing", backend="sentry", endpoint=url)
        return True
    except ImportError as e:
        log.warn(
            "lane_disabled",
            what="tracing",
            reason="opentelemetry not installed - rebuild the venv",
            err=str(e),
        )
        return False
    except Exception as e:
        log.error("tracing_setup_failed", err=repr(e))
        return False


def setup(cfg, log):
    """Configure Sentry and return whether traces are enabled."""
    global _on
    _on = False
    try:
        if not config.real_key(cfg.get("sentryDsn")):
            log(
                "lane_disabled",
                what="sentry",
                reason="sentryDsn not set in config.json",
            )
            return False
        dsn = cfg["sentryDsn"]
        return _trace(dsn, log) if _init(dsn, log) else False
    except Exception as e:
        log.error("sentry_setup_failed", err=repr(e))
        return False


def capture(exc):
    """Report a handled exception when the Sentry SDK is available."""
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def span_attributes(session=None, turn=None):
    """Attributes for the conversation (root) span - the only one pipecat
    applies them to, and the only place its API takes ours. genai stamps the
    gen_ai spans underneath from the ids session_trace pinned."""
    ctx = events.current()
    session = session or ctx.get("session")
    turn = turn or ctx.get("turn")
    attrs = {
        "user.id": events.HOST,
        "env": events.ENV,
    }
    if session:
        attrs["session.id"] = session
        # Group each voice session as one Sentry conversation.
        attrs["gen_ai.conversation.id"] = session
    if turn:
        # Link the trace to the log entry that opened the session.
        attrs["couch.turn"] = turn
    return attrs


@contextlib.contextmanager
def session_trace():
    """Use one trace ID for a voice session's spans and local log entries.

    Yields the hexadecimal trace ID, or ``None`` when tracing is unavailable.
    """
    if not _on:
        yield None
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace as _otel

        trace_id = random.getrandbits(128)
        parent = _otel.NonRecordingSpan(
            _otel.SpanContext(
                trace_id=trace_id,
                span_id=random.getrandbits(64),
                is_remote=True,
                trace_flags=_otel.TraceFlags(_otel.TraceFlags.SAMPLED),
            )
        )
        otel_token = otel_context.attach(_otel.set_span_in_context(parent))
        hex_id = _otel.format_trace_id(trace_id)
    except Exception:
        yield None
        return
    # The export thread needs its own copy of the session ID.
    genai.set_session(events.current().get("session"))
    events_token = events.context(trace=hex_id)
    try:
        yield hex_id
    finally:
        genai.set_session()
        events.reset(events_token)
        try:
            otel_context.detach(otel_token)
        except Exception:
            pass


def set_turn(turn):
    """Point the next spans at this utterance. Called where the turn id is
    born; no-ops harmlessly when tracing is off."""
    genai.set_turn(turn)


def conversation_id():
    """Pipecat's conversation id = our session id: one voice session is one
    Sentry Conversation and one `session` in the JSONL."""
    return events.current().get("session") or events.new_turn()


# --- the text lane's own LLM calls -------------------------------------------
# brain/backends.py drives the Anthropic and OpenAI SDKs directly, outside
# pipecat, so nothing else traces it. These build the same spans genai.reshape
# rewrites pipecat's into - shaped at creation, because we own them.
#
# All of it no-ops while _on is False, which is what keeps the --text REPL out
# of production Conversations: voice_agent returns before setup() runs.

# Short key -> Sentry attribute. The caller flattens its SDK's usage object,
# so nothing here knows an Anthropic field from an OpenAI one.
USAGE_ATTR = {
    "input": "gen_ai.usage.input_tokens",
    "output": "gen_ai.usage.output_tokens",
    "cache_read": "gen_ai.usage.cache_read.input_tokens",
    "cache_write": "gen_ai.usage.cache_creation.input_tokens",
    "reasoning": "gen_ai.usage.reasoning.output_tokens",
}


class _Chat:
    """What chat_span yields. Holds a live span, or None when tracing is off,
    so the caller writes the same code either way."""

    def __init__(self, span=None):
        self.span = span

    def response(self, model=None, output=None, usage=None):
        """Record the response half. Must be called INSIDE the with block -
        attributes set on an ended span are dropped."""
        if self.span is None:
            return
        try:
            if model:
                self.span.set_attribute("gen_ai.response.model", str(model))
            if output:
                self.span.set_attribute(
                    "gen_ai.output.messages",
                    json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [{"type": "text", "content": str(output)}],
                            }
                        ]
                    ),
                )
            for key, value in (usage or {}).items():
                if value is not None and key in USAGE_ATTR:
                    self.span.set_attribute(USAGE_ATTR[key], int(value))
        except Exception:
            pass


def _tracer():
    from opentelemetry import trace as _otel

    return _otel.get_tracer("slopstation.llm")


def agent(name):
    """Decorator: wrap a call in the gen_ai.invoke_agent span Sentry's Agents
    dashboard hangs chat and tool spans under. A decorator rather than a with
    block so one turn's whole tool loop is covered without re-indenting it."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not _on:
                return fn(*a, **kw)
            try:
                span = _tracer().start_as_current_span(
                    f"invoke_agent {name}",
                    attributes={
                        "sentry.op": "gen_ai.invoke_agent",
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": name,
                    },
                )
            except Exception:
                return fn(*a, **kw)
            with span:
                return fn(*a, **kw)

        return wrapper

    return deco


@contextlib.contextmanager
def chat_span(provider, model, messages=None, tools=None, system=None):
    """One request to an LLM, in Sentry's gen_ai.chat shape.

    messages and tools are the SDK's own lists; they are JSON-encoded here
    because span attributes take primitives only, and Sentry accepts the
    legacy {role, content} message form both SDKs already use.
    """
    span = None
    if _on:
        try:
            attrs = {
                "sentry.op": "gen_ai.chat",
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model,
            }
            if system:
                attrs["gen_ai.system_instructions"] = str(system)
            if messages:
                attrs["gen_ai.input.messages"] = json.dumps(messages, default=str)
            if tools:
                attrs["gen_ai.tool.definitions"] = json.dumps(tools, default=str)
            span = _tracer().start_span(f"chat {model}", attributes=attrs)
        except Exception:
            span = None
    try:
        yield _Chat(span)
    finally:
        if span is not None:
            try:
                span.end()
            except Exception:
                pass


def tool_span(kind, query, result=None):
    """Record a provider-executed tool call under the active span."""
    if not _on:
        return
    try:
        with _tracer().start_as_current_span(
            f"execute_tool {kind}",
            attributes={
                "sentry.op": "gen_ai.execute_tool",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": str(kind),
                "gen_ai.tool.type": "function",
                "gen_ai.tool.call.arguments": str(query)[:2000],
            },
        ) as s:
            if result:
                s.set_attribute("gen_ai.tool.call.result", str(result))
    except Exception:
        pass
