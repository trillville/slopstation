"""Ships the voice pipeline's Pipecat spans to Sentry.

Pipecat emits the tree itself - conversation (one voice session) -> turn ->
stt/llm/tts, with token counts, time to first byte, transcripts and TTS
character counts
already attached; this module is only plumbing. Enabled by the presence of
the config block; every entry point returns a bool and swallows everything,
so a bad key or a dead uplink costs traces, never voice. Spans carry
transcripts and completions verbatim - events.scrub() does not apply (Pipecat
builds these attributes) and no secret may become a span attribute.

Sentry's 20 kB cap on gen_ai.input.messages is an SDK-internal constant and
does not reach spans arriving over OTLP, which is this path; measured span
payloads sit three orders of magnitude under it either way.
"""
import logging
import os

import checkin
import events

# Sentry's OTLP ingest. Open beta, HTTP only, and the path is used verbatim -
# the SDK's "append /v1/traces" convention applies to the env-var endpoint,
# not to an explicit one.
TRACES_PATH = "/api/{project}/integration/otlp/v1/traces"

_on = False


def is_on():
    """True once setup() has succeeded. Callers gate on this rather than
    re-deriving, so a mid-run config change cannot half-enable the
    pipeline."""
    return _on


def enabled(cfg):
    """Keyed on config.json, not secrets.json: the DSN public key is public
    by design and the cron check-ins need it in a URL."""
    return checkin.sentry_config(cfg) is not None


def auth_header(public_key):
    """Sentry's OTLP auth. One header, no base64 - the DSN public key
    identifies the project and carries no write scope beyond ingest."""
    return f"sentry sentry_key={public_key}"


def endpoint(cfg):
    parts = checkin.sentry_config(cfg)
    if not parts:
        return ""
    _, project, _ = parts
    return (f"https://{checkin.ingest_host(cfg)}"
            + TRACES_PATH.format(project=project))


def _exporter(cfg):
    """OTLP/HTTP: Sentry does not accept gRPC (pipecat's own tracing example
    imports the gRPC exporter, so the http pin in requirements.txt is what
    keeps it from resolving by accident)."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter)
    parts = checkin.sentry_config(cfg)
    return OTLPSpanExporter(
        endpoint=endpoint(cfg),
        headers={"x-sentry-auth": auth_header(parts[2])},
    )


def setup(cfg, log):
    """Wire Pipecat's tracing to Sentry. True if traces will flow; missing
    opentelemetry is a warn and a False, not a crash."""
    global _on
    if not enabled(cfg):
        log("lane_disabled", what="tracing",
            reason="no sentry block in config.json")
        return False
    try:
        from pipecat.utils.tracing.setup import setup_tracing

        # Sentry reads the deployment.environment resource attribute, which
        # pipecat builds from os.getenv("ENVIRONMENT", "development").
        # setdefault so an explicit shell value still wins.
        os.environ.setdefault("ENVIRONMENT", events.ENV)

        # WARNING, not CRITICAL: export errors (wrong key, wrong org) are the
        # only signal that traces are misconfigured.
        logging.getLogger("opentelemetry").setLevel(logging.WARNING)

        ok = setup_tracing(service_name="slopstation-voice",
                           exporter=_exporter(cfg),
                           console_export=False)
        _on = bool(ok)
        if _on:
            log("lane_up", what="tracing", backend="sentry",
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
    applies them to. Scalars only: Sentry ingests a list attribute but cannot
    search, filter or aggregate on one, so a tags array would be dead
    weight."""
    ctx = events.current()
    session = session or ctx.get("session")
    turn = turn or ctx.get("turn")
    attrs = {
        "user.id": events.HOST,
        "env": events.ENV,
    }
    if session:
        attrs["session.id"] = session
        # What groups turns into one browsable conversation in Sentry.
        attrs["gen_ai.conversation.id"] = session
    if turn:
        # The turn that OPENED the session: the join back to the JSONL, not a
        # per-turn label (Pipecat numbers its turn spans independently).
        attrs["couch.turn"] = turn
    return attrs


def conversation_id():
    """Pipecat's conversation id = our session id: one voice session is one
    Sentry conversation and one `session` in the JSONL."""
    return events.current().get("session") or events.new_turn()


def tool_span(kind, query, result=None):
    """A provider-executed tool call, parented to whatever span is open (in
    the voice pipeline, Pipecat's llm span). Pipecat cannot do this itself:
    server-side tools are not in its Responses handler (see llm_audit.py)."""
    if not _on:
        return
    try:
        from opentelemetry import trace as _otel
        tracer = _otel.get_tracer("slopstation.llm")
        # Name and op both matter: Sentry's Agents view counts tool calls by
        # the gen_ai.execute_tool operation, not by span name.
        with tracer.start_as_current_span(f"execute_tool {kind}") as s:
            s.set_attribute("gen_ai.operation.name", "execute_tool")
            s.set_attribute("gen_ai.tool.name", str(kind))
            s.set_attribute("gen_ai.tool.call.arguments", str(query)[:2000])
            if result:
                s.set_attribute("gen_ai.tool.call.result", str(result))
    except Exception:
        pass
