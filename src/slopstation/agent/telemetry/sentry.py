"""Ships the agent lane to Sentry: crashes as Issues, the voice pipeline as
traces, and the LLM spans agent monitoring reads.

One sentry_sdk.init, in this order and for these reasons:

  1. init FIRST, so crashes are reported even if the trace pipeline is
     broken. A dead exporter is not a reason to stop hearing about
     exceptions.
  2. pipecat's setup_tracing() installs the global TracerProvider. It calls
     set_tracer_provider unconditionally, so anything installed before it is
     discarded - it has to run before our exporter, not after.
  3. our exporter goes on that provider.

`OTLPIntegration(setup_otlp_traces_exporter=False)` is deliberate. The half we
want from it is event linking: an exception attaches to the OTel span it broke
on, which is the whole reason errors and traces are worth having in one
place. The half we decline is its exporter, because genai.SentryShape has to
wrap ours - two would double-ship every span. The endpoint and auth still come
from the SDK's own DSN helper, the same call the integration makes, so a
region or path change upstream is picked up rather than re-derived here.

Enabled by config.json's `sentryDsn` alone: no secrets.json key, because a
DSN's public half is not a secret - it ships inside client apps by design.

Every entry point returns a bool and swallows everything: a bad DSN or a dead
uplink costs telemetry, never voice.

Spans carry transcripts and completions VERBATIM. events.scrub() does not
apply - pipecat builds those attributes - so no secret may become a span
attribute. send_default_pii is what turns that capture on; without it Sentry
drops every prompt and reply.
"""
import contextlib
import functools
import json
import logging
import random

from slopstation import cglib
from slopstation import events
from slopstation.agent.telemetry import genai

# One Sentry project takes both machines and every lane, so this is what
# separates the voice pipeline's traces inside it.
SERVICE_NAME = "slopstation-voice"

_on = False


def is_on():
    """True once tracing is wired. Callers gate on this rather than
    re-deriving, so a mid-run config change cannot half-enable the pipeline."""
    return _on


def enabled(cfg):
    """A real DSN in config.json. Template junk reads as absent, the same rule
    as every other keyed lane."""
    return cglib.real_key(cfg.get("sentryDsn"))


def otlp_target(dsn):
    """(url, headers) for Sentry's OTLP traces endpoint, from the SDK's own
    DSN helper - the same call OTLPIntegration makes, so a region or path
    change upstream is picked up rather than re-derived here."""
    from sentry_sdk.consts import VERSION, EndpointType
    from sentry_sdk.utils import Dsn
    auth = Dsn(dsn).to_auth(f"sentry.python/{VERSION}")
    return (auth.get_api_url(EndpointType.OTLP_TRACES),
            {"X-Sentry-Auth": auth.to_header()})


def _exporter(url, headers):
    """The OTLP exporter OTLPIntegration would have built, wrapped so genai
    can reshape spans on the way out."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter)
    return genai.SentryShape(OTLPSpanExporter(endpoint=url, headers=headers))


def _init(dsn, log):
    """sentry_sdk.init: errors, and the linking that puts them on the right
    span. True if the SDK is live."""
    try:
        import sentry_sdk
        from sentry_sdk.integrations.otlp import OTLPIntegration
        sentry_sdk.init(
            dsn=dsn,
            environment=events.ENV,
            integrations=[OTLPIntegration(setup_otlp_traces_exporter=False)],
            # Transcripts and completions are the point of the traces; without
            # this Sentry drops every prompt and reply.
            send_default_pii=True,
            # Logs reach Sentry through the collector reading the JSONL. The
            # SDK must not ship a second copy of the same lines.
            enable_logs=False,
        )
        # Populates the User column in Conversations. The host, not a person:
        # this is a house, and one box is one user.
        sentry_sdk.set_user({"id": events.HOST})
        log("lane_up", what="sentry", kind="errors")
        return True
    except ImportError as e:
        log.warn("lane_disabled", what="sentry",
                 reason="sentry-sdk not installed - rebuild the venv",
                 err=str(e))
        return False
    except Exception as e:
        log.error("sentry_setup_failed", err=repr(e))
        return False


def _trace(dsn, log):
    """Pipecat's tracer provider, and our exporter on top of it."""
    global _on
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from pipecat.utils.tracing.setup import setup_tracing

        # WARNING, not CRITICAL: export errors - a wrong DSN, a dead uplink -
        # are the only signal that traces are misconfigured.
        logging.getLogger("opentelemetry").setLevel(logging.WARNING)

        if not setup_tracing(service_name=SERVICE_NAME, console_export=False):
            log.warn("lane_disabled", what="tracing",
                     reason="pipecat setup_tracing returned false")
            return False
        url, headers = otlp_target(dsn)
        provider = trace.get_tracer_provider()
        provider.add_span_processor(BatchSpanProcessor(_exporter(url, headers)))
        _on = True
        log("lane_up", what="tracing", backend="sentry", endpoint=url)
        return True
    except ImportError as e:
        log.warn("lane_disabled", what="tracing",
                 reason="opentelemetry not installed - rebuild the venv",
                 err=str(e))
        return False
    except Exception as e:
        log.error("tracing_setup_failed", err=repr(e))
        return False


def setup(cfg, log):
    """Wire the agent lane to Sentry. True when TRACES will flow; errors can
    be on while this returns False, which is deliberate - a broken trace
    pipeline is not a reason to stop reporting crashes.

    The last try/except is the boundary the whole module promises: voice_agent
    calls this bare, before the wake loop, so anything that escapes here is an
    agent that will not start."""
    global _on
    _on = False
    try:
        if not enabled(cfg):
            log("lane_disabled", what="sentry",
                reason="sentryDsn not set in config.json")
            return False
        dsn = cfg["sentryDsn"]
        return _trace(dsn, log) if _init(dsn, log) else False
    except Exception as e:
        log.error("sentry_setup_failed", err=repr(e))
        return False


def capture(exc):
    """Report an exception the lane has already handled, so a swallowed error
    still reaches Issues with its stack. No-ops when the SDK is absent."""
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
        # What Sentry groups a Conversation on. Same value as session.id -
        # one voice session is one Conversation.
        attrs["gen_ai.conversation.id"] = session
    if turn:
        # The turn that OPENED the session: the join back to the JSONL, not a
        # per-turn label (pipecat numbers its turn spans independently).
        attrs["couch.turn"] = turn
    return attrs


@contextlib.contextmanager
def session_trace():
    """Pin one trace id across a voice session, in the spans AND in the JSONL,
    so clicking a log line in Sentry opens that session's waterfall.

    The id has to exist BEFORE the pipeline starts, because events.context is
    inherited by asyncio.run and cannot be back-filled once tasks are running.
    So rather than opening a span of our own, this attaches a non-recording
    REMOTE parent - the standard OTel way to continue a trace started
    elsewhere. Pipecat's conversation span becomes its child and inherits the
    id: nothing extra is exported, and the parent Sentry never sees is the
    ordinary orphan-trace case.

    Yields 32 hex, or None when tracing is off or anything goes wrong. Both
    contexts are reset on the way out - a `trace` field outliving its span
    would join log lines to a waterfall they were not part of.
    """
    if not _on:
        yield None
        return
    try:
        from opentelemetry import context as otel_context, trace as _otel
        trace_id = random.getrandbits(128)
        parent = _otel.NonRecordingSpan(_otel.SpanContext(
            trace_id=trace_id, span_id=random.getrandbits(64), is_remote=True,
            trace_flags=_otel.TraceFlags(_otel.TraceFlags.SAMPLED)))
        otel_token = otel_context.attach(
            _otel.set_span_in_context(parent))
        hex_id = _otel.format_trace_id(trace_id)
    except Exception:
        yield None
        return
    # The export thread cannot read events.current(), so the id pipecat's
    # spans get stamped with is pinned here for the session's length. The TURN
    # is not knowable yet - it is minted per utterance inside the pipeline, so
    # grammar_gate calls set_turn as each one is born.
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
                self.span.set_attribute("gen_ai.output.messages", json.dumps(
                    [{"role": "assistant",
                      "parts": [{"type": "text", "content": str(output)}]}]))
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
                    attributes={"sentry.op": "gen_ai.invoke_agent",
                                "gen_ai.operation.name": "invoke_agent",
                                "gen_ai.agent.name": name})
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
            attrs = {"sentry.op": "gen_ai.chat",
                     "gen_ai.operation.name": "chat",
                     "gen_ai.provider.name": provider,
                     "gen_ai.request.model": model}
            if system:
                attrs["gen_ai.system_instructions"] = str(system)
            if messages:
                attrs["gen_ai.input.messages"] = json.dumps(messages,
                                                            default=str)
            if tools:
                attrs["gen_ai.tool.definitions"] = json.dumps(tools,
                                                              default=str)
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
    """A provider-executed tool call, parented to whatever span is open (in
    the voice pipeline, pipecat's llm span). Pipecat cannot do this itself:
    server-side tools are not in its Responses handler (see llm_audit.py).

    Shaped at creation rather than by genai.reshape - we own these, so there
    is nothing to adapt."""
    if not _on:
        return
    try:
        from opentelemetry import trace as _otel
        tracer = _otel.get_tracer("slopstation.llm")
        with tracer.start_as_current_span(
                f"execute_tool {kind}",
                attributes={
                    "sentry.op": "gen_ai.execute_tool",
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": str(kind),
                    "gen_ai.tool.type": "function",
                    "gen_ai.tool.call.arguments": str(query)[:2000],
                }) as s:
            if result:
                s.set_attribute("gen_ai.tool.call.result", str(result))
    except Exception:
        pass
