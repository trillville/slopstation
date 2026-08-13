"""Agent traces: the voice pipeline's spans, shipped to Langfuse.

Pipecat emits the whole tree itself once tracing is on - conversation -> turn
-> stt/llm/tts - with token counts, TTFB per service, transcripts and TTS
character counts already on the spans. So this module is *only* plumbing:
build an exporter, hand it to Pipecat, and make every part of that fail soft.
Almost nothing here is our own instrumentation, which is the point - the
framework's spans are better than ours would be and they cannot drift from
the pipeline they describe.

WHAT LANDS IN LANGFUSE
    conversation                one voice session (Pipecat's model: one trace
    |                           IS one conversation, not one turn)
    +- turn                     turn.number, turn.duration_seconds,
    |  |                        turn.was_interrupted
    |  +- stt                   transcript, ttfb
    |  +- llm                   model, gen_ai.usage.*_tokens, ttfb
    |  +- tts                   voice_id, text, character_count, ttfb

THE RULES THIS OBEYS
    * Telemetry never costs a session. A missing key, a bad key, a dead
      uplink, or opentelemetry not being installed at all must degrade to
      "no traces" and never to "no voice". Every entry point returns a bool
      and swallows everything.
    * Off by default in the absence of keys, exactly like the assistant and
      worker lanes - real_key() decides, no extra config flag to forget.

PRIVACY: spans carry transcripts and completions verbatim - the recorded
decision is to ship content, since a trace without the words is not worth
having in a single-household system. events.scrub() does NOT apply here: it
guards our own JSONL fields, and these attributes are built by Pipecat.
Acceptable because no secret is ever a span attribute - what flows is speech,
model names, token counts and timings. Keep it that way: an API key or the
VirtualHere PIN reaching a span would leave the house unredacted.
"""
import base64
import contextlib
import logging
import os

import cglib
import events

# Region matters: Langfuse is per-account and the US host is a different
# domain, not a path. EU is cloud.langfuse.com, US is us.cloud.langfuse.com.
DEFAULT_HOST = "https://us.cloud.langfuse.com"

# Signal-specific path. The base OTLP endpoint gets "/v1/traces" appended by
# the SDK only when it comes from OTEL_EXPORTER_OTLP_ENDPOINT; an explicit
# endpoint= is used verbatim, so it has to be the full path.
TRACES_PATH = "/api/public/otel/v1/traces"

_on = False


def is_on():
    """True once setup() has succeeded. run_session gates on this rather than
    re-deriving, so a mid-run key change cannot half-enable the pipeline."""
    return _on


def enabled(secrets):
    return (cglib.real_key(secrets.get("langfusePublicKey"))
            and cglib.real_key(secrets.get("langfuseSecretKey")))


def auth_header(public_key, secret_key):
    """Basic auth over the key pair, as Langfuse's OTLP endpoint expects.

    Note the plain space: the '%20' that Langfuse's docs show belongs to the
    OTEL_EXPORTER_OTLP_HEADERS *environment variable* format, where a literal
    space would split the header list. We pass a dict straight to the
    exporter, so the value is used verbatim and the escape would corrupt it.
    """
    token = base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def endpoint(cfg):
    host = cfg.get("voice", {}).get("langfuseHost") or DEFAULT_HOST
    return host.rstrip("/") + TRACES_PATH


def _exporter(cfg, secrets):
    """Langfuse over OTLP/HTTP. HTTP is not a preference - Langfuse does not
    accept gRPC at all, and Pipecat's own example imports the gRPC exporter,
    so this is the single easiest thing to get silently wrong."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter)
    return OTLPSpanExporter(
        endpoint=endpoint(cfg),
        headers={
            "Authorization": auth_header(secrets["langfusePublicKey"],
                                         secrets["langfuseSecretKey"]),
            # Real-time ingestion rather than the batch path.
            "x-langfuse-ingestion-version": "4",
        },
    )


def setup(cfg, secrets, log):
    """Wire Pipecat's tracing to Langfuse. Returns True if traces will flow.

    Safe to call when opentelemetry is not installed (the venv predates the
    pins) - that is a warn and a False, not a crash, so a K15 that pulled the
    code but has not rebuilt its venv still runs voice normally.
    """
    global _on
    if not enabled(secrets):
        log("lane_disabled", what="tracing",
            reason="langfuse keys not set in secrets.json")
        return False
    try:
        from pipecat.utils.tracing.setup import setup_tracing

        # Langfuse's "Env" badge reads the deployment.environment RESOURCE
        # attribute, which pipecat builds from os.getenv("ENVIRONMENT",
        # "development") - without this every real session files under
        # "development". events.ENV keeps ONE vocabulary across Loki and
        # Langfuse; setdefault so an explicit shell value still wins.
        os.environ.setdefault("ENVIRONMENT", events.ENV)

        # Export failures stay VISIBLE. Silencing this logger to CRITICAL
        # quiets a dead uplink's per-minute stack trace, but also hides the
        # only message that says "wrong keys" or "wrong region" - noise is
        # recoverable, a silent misconfiguration is not. WARNING keeps the
        # per-export chatter down without touching the errors that matter.
        logging.getLogger("opentelemetry").setLevel(logging.WARNING)

        ok = setup_tracing(service_name="slopstation-voice",
                           exporter=_exporter(cfg, secrets),
                           console_export=False)
        _on = bool(ok)
        if _on:
            log("lane_up", what="tracing", backend="langfuse",
                endpoint=endpoint(cfg))
        else:
            log.warn("lane_disabled", what="tracing",
                     reason="pipecat setup_tracing returned false")
        return _on
    except ImportError as e:
        log.warn("lane_disabled", what="tracing",
                 reason="opentelemetry not installed - rebuild the venv",
                 err=str(e))
        return False
    except Exception as e:
        log.error("tracing_setup_failed", err=repr(e))
        return False


def span_attributes(session=None, turn=None):
    """Attributes for the conversation (root) span - the only one Pipecat
    applies them to, and the one Langfuse reads trace-level fields from.
    Without langfuse.trace.name every trace in the list reads "conversation"
    or a bare UUID.

    Both spellings of session/user are set: langfuse.* is what Langfuse
    reads, session.id/user.id are the OTel-conventional names another backend
    would look for."""
    ctx = events.current()
    session = session or ctx.get("session")
    turn = turn or ctx.get("turn")
    attrs = {
        "langfuse.trace.name": "voice session",
        "langfuse.user.id": events.HOST,
        "user.id": events.HOST,
        "langfuse.trace.tags": ["couch", "voice"],
        "env": events.ENV,
    }
    if session:
        attrs["langfuse.session.id"] = session
        attrs["session.id"] = session
    if turn:
        # The turn that OPENED the session. Later turns get their own ids in
        # the JSONL; Pipecat numbers its own turn spans independently, so this
        # is the join back to the logs, not a per-turn label.
        attrs["couch.turn"] = turn
    return attrs


def conversation_id():
    """Pipecat's conversation id = our session id, so one voice session is one
    Langfuse trace AND one `session` in the JSONL. That shared value is what
    lets you jump from a trace to the logs around it."""
    return events.current().get("session") or events.new_turn()


# --- Tier-3 background jobs ---------------------------------------------------
#
# A job is queued during a conversation and finishes minutes later on the
# worker thread, long after the conversation's spans have closed - and the
# worker is a subprocess outside the pipeline Pipecat traces, so "what did
# the agent actually DO for three minutes" had no answer in either system.
#
# W3C trace context fixes it: carrier() freezes the span active when the tool
# fired, job_span() re-parents the worker's spans onto it. Same mechanism as
# cross-service tracing, across a thread and a few minutes instead.
#
# ONE CONSEQUENCE, accepted: the conversation's trace latency becomes
# wall-clock to job completion, so a 91 s conversation that queued a 3-minute
# job reads as ~3 minutes. Truer (the request was not finished until the
# announcement), but not the number that was there before.


def carrier():
    """Freeze the active span context into a W3C traceparent dict, or None.

    Stored on the job record, so it also survives a restart mid-job - the
    reconciler picks the job back up and its spans still find their parent."""
    if not _on:
        return None
    try:
        from opentelemetry.propagate import inject
        out = {}
        inject(out)
        return out or None
    except Exception:
        return None


class _NullJob:
    """What every failure path yields: same shape, does nothing."""

    def step(self, *a, **kw):
        pass

    def finish(self, *a, **kw):
        pass


class _Job:
    def __init__(self, tracer, span):
        self._tracer, self._span = tracer, span

    def step(self, tool, arg="", result=""):
        """One tool call the worker made. The CLI's stream carries no
        per-tool timings, so these are point-in-time spans in call order -
        WHAT it did and with what, not how long each took."""
        try:
            with self._tracer.start_as_current_span(f"tool: {tool}") as s:
                s.set_attribute("langfuse.observation.type", "tool")
                s.set_attribute("gen_ai.tool.name", str(tool))
                if arg:
                    s.set_attribute("langfuse.observation.input", str(arg)[:2000])
                if result:
                    s.set_attribute("langfuse.observation.output",
                                    str(result)[:2000])
        except Exception:
            pass

    def finish(self, summary="", detail="", meta=None):
        try:
            if summary or detail:
                self._span.set_attribute(
                    "langfuse.observation.output",
                    (f"{summary}\n\n{detail}" if detail else summary)[:8000])
            for k, v in (meta or {}).items():
                self._span.set_attribute(f"couch.job.{k}", v)
            # gen_ai.* as well, so the usage reads as usage rather than as
            # opaque custom fields.
            for src, dst in (("input_tokens", "gen_ai.usage.input_tokens"),
                             ("output_tokens", "gen_ai.usage.output_tokens"),
                             ("model", "gen_ai.request.model")):
                if (meta or {}).get(src) is not None:
                    self._span.set_attribute(dst, meta[src])
        except Exception:
            pass


def tool_span(kind, query, status=None):
    """A provider-executed tool call, recorded where it happened.

    Point-in-time and parented to whatever span is open - inside the voice
    pipeline that is Pipecat's llm span, so a search lands beside the turn
    that ran it. Pipecat cannot do this itself: server-side tools are not in
    its Responses handler at all (see llm_audit.py)."""
    if not _on:
        return
    try:
        from opentelemetry import trace as _otel
        tracer = _otel.get_tracer("slopstation.llm")
        with tracer.start_as_current_span(f"tool: {kind}") as s:
            s.set_attribute("langfuse.observation.type", "tool")
            s.set_attribute("gen_ai.tool.name", str(kind))
            s.set_attribute("langfuse.observation.input", str(query)[:2000])
            if status:
                s.set_attribute("langfuse.observation.output", str(status))
    except Exception:
        pass


@contextlib.contextmanager


def job_span(job_id, task, trace_carrier=None, session=None, provider=""):
    """Span for one background job, re-parented onto the conversation.

    Never raises and never suppresses: a tracing failure yields _NullJob and
    the job runs exactly as before, while an exception from the job body
    propagates untouched."""
    cm = helper = None
    if _on:
        try:
            from opentelemetry import trace as _otel
            from opentelemetry.propagate import extract
            tracer = _otel.get_tracer("slopstation.jobs")
            cm = tracer.start_as_current_span(
                "background task", context=extract(trace_carrier or {}))
            span = cm.__enter__()
            span.set_attribute("langfuse.observation.type", "span")
            span.set_attribute("langfuse.observation.input", str(task)[:8000])
            span.set_attribute("couch.job.id", str(job_id))
            if provider:
                span.set_attribute("couch.job.provider", str(provider))
            sess = session or events.current().get("session")
            if sess:
                # Repeated on the span on purpose: Langfuse filters sessions
                # off whatever spans carry it, and a job that outlived its
                # conversation must still land in the same session.
                span.set_attribute("langfuse.session.id", sess)
                span.set_attribute("session.id", sess)
            helper = _Job(tracer, span)
        except Exception:
            cm = helper = None
    try:
        yield helper or _NullJob()
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


# TODO(E5b): dual-export the same spans to Grafana Tempo. The datasource and
# the traces:write scope already exist; what is missing is the OTLP endpoint
# from the stack's OpenTelemetry tile. setup_tracing takes ONE exporter, so
# this means grabbing the provider afterwards and adding a second
# BatchSpanProcessor - deliberately deferred rather than half-built, because
# Langfuse is where these traces actually get read.
