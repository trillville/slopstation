"""Ships the voice pipeline's Pipecat spans to Langfuse.

Pipecat emits the tree itself - conversation (one voice session) -> turn ->
stt/llm/tts, with token counts, time to first byte, transcripts and TTS
character counts
already attached; this module is only plumbing. Enabled by the presence of
keys; every entry point returns a bool and swallows everything, so a bad key
or a dead uplink costs traces, never voice. Spans carry transcripts and
completions verbatim - events.scrub() does not apply (Pipecat builds these
attributes) and no secret may become a span attribute.
"""
import base64
import contextlib
import logging
import os

import cglib
import events

# Region is a domain, not a path (EU: cloud.langfuse.com).
DEFAULT_HOST = "https://us.cloud.langfuse.com"

# Full signal path: the SDK appends "/v1/traces" only to
# OTEL_EXPORTER_OTLP_ENDPOINT; an explicit endpoint= is used verbatim.
TRACES_PATH = "/api/public/otel/v1/traces"

_on = False


def is_on():
    """True once setup() has succeeded. Callers gate on this rather than
    re-deriving, so a mid-run key change cannot half-enable the pipeline."""
    return _on


def enabled(secrets):
    return (cglib.real_key(secrets.get("langfusePublicKey"))
            and cglib.real_key(secrets.get("langfuseSecretKey")))


def auth_header(public_key, secret_key):
    """Basic auth over the key pair. Plain space, not the '%20' in Langfuse's
    docs - that escape belongs to the OTEL_EXPORTER_OTLP_HEADERS env-var
    format; a dict value is used verbatim."""
    token = base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def endpoint(cfg):
    host = cfg.get("voice", {}).get("langfuseHost") or DEFAULT_HOST
    return host.rstrip("/") + TRACES_PATH


def _exporter(cfg, secrets):
    """OTLP/HTTP: Langfuse does not accept gRPC (Pipecat's own tracing example
    imports the gRPC exporter)."""
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
    """Wire Pipecat's tracing to Langfuse. True if traces will flow; missing
    opentelemetry is a warn and a False, not a crash."""
    global _on
    if not enabled(secrets):
        log("lane_disabled", what="tracing",
            reason="langfuse keys not set in secrets.json")
        return False
    try:
        from pipecat.utils.tracing.setup import setup_tracing

        # Langfuse's "Env" badge reads the deployment.environment resource
        # attribute, which pipecat builds from os.getenv("ENVIRONMENT",
        # "development"). setdefault so an explicit shell value still wins.
        os.environ.setdefault("ENVIRONMENT", events.ENV)

        # WARNING, not CRITICAL: export errors (wrong keys, wrong region) are
        # the only signal that traces are misconfigured.
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
    applies them to, and where Langfuse reads trace-level fields. Without
    langfuse.trace.name traces list as "conversation" or a bare UUID. Both
    spellings: langfuse.* for Langfuse, session.id/user.id for OTel."""
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
        # The turn that OPENED the session: the join back to the JSONL, not a
        # per-turn label (Pipecat numbers its turn spans independently).
        attrs["couch.turn"] = turn
    return attrs


def conversation_id():
    """Pipecat's conversation id = our session id: one voice session is one
    Langfuse trace and one `session` in the JSONL."""
    return events.current().get("session") or events.new_turn()


# --- background jobs ----------------------------------------------------------
#
# A job finishes minutes after the conversation's spans close, in a worker
# subprocess outside the pipeline Pipecat traces. W3C trace context bridges
# it: carrier() freezes the span active when the tool fired, job_span()
# re-parents the worker's spans onto it. So the conversation's trace latency
# reads as wall-clock to job completion.


def carrier():
    """Freeze the active span context into a W3C traceparent dict, or None.
    Stored on the job record, so a job resumed after a restart still finds its
    parent."""
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
        """One tool call the worker made. Point-in-time, in call order: the
        CLI's stream carries no per-tool timings."""
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
            # gen_ai.* too, so usage reads as usage rather than custom fields.
            for src, dst in (("input_tokens", "gen_ai.usage.input_tokens"),
                             ("output_tokens", "gen_ai.usage.output_tokens"),
                             ("model", "gen_ai.request.model")):
                if (meta or {}).get(src) is not None:
                    self._span.set_attribute(dst, meta[src])
        except Exception:
            pass


def tool_span(kind, query, result=None):
    """A provider-executed tool call, parented to whatever span is open (in
    the voice pipeline, Pipecat's llm span). Pipecat cannot do this itself:
    server-side tools are not in its Responses handler (see llm_audit.py)."""
    if not _on:
        return
    try:
        from opentelemetry import trace as _otel
        tracer = _otel.get_tracer("slopstation.llm")
        with tracer.start_as_current_span(f"tool: {kind}") as s:
            s.set_attribute("langfuse.observation.type", "tool")
            s.set_attribute("gen_ai.tool.name", str(kind))
            s.set_attribute("langfuse.observation.input", str(query)[:2000])
            if result:
                s.set_attribute("langfuse.observation.output", str(result))
    except Exception:
        pass


@contextlib.contextmanager
def job_span(job_id, task, trace_carrier=None, session=None, provider=""):
    """Span for one background job, re-parented onto the conversation. Never
    raises and never suppresses: a tracing failure yields _NullJob, an
    exception from the job body propagates untouched."""
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
                # Repeated on the span: Langfuse filters sessions off whatever
                # spans carry it, and a job can outlive its conversation.
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

