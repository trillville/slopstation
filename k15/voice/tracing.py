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

PRIVACY: spans carry transcripts and completions verbatim. That is the
recorded decision (observability-design.md, position 1: ship content). Note
that events.scrub() does NOT apply here - it guards our own JSONL fields, and
these attributes are built by Pipecat. Acceptable because no secret is ever a
span attribute: what flows is speech, model names, token counts and timings.
"""
import base64
import logging

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

        # OTel's exporters log every failed export at ERROR through stdlib
        # logging, which would put a stack trace on the console once a minute
        # behind a dead uplink. The BatchSpanProcessor already drops and
        # retries on its own; we do not need to hear about it.
        logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)

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
    """Attributes for the conversation (root) span.

    Pipecat only applies these to the top-level span, which is also the one
    Langfuse reads trace-level fields from - so this is where the trace gets
    a usable name and an identity. Without langfuse.trace.name every trace in
    the list is called "conversation" or a bare UUID, which makes the session
    list useless at exactly the moment you need it.

    Both spellings of session/user are set on purpose: langfuse.* is what
    Langfuse reads, session.id/user.id are the OTel-conventional names any
    other backend would look for. Cheap, and it keeps the escape hatch open.
    """
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


# TODO(E5b): dual-export the same spans to Grafana Tempo. The datasource and
# the traces:write scope already exist; what is missing is the OTLP endpoint
# from the stack's OpenTelemetry tile. setup_tracing takes ONE exporter, so
# this means grabbing the provider afterwards and adding a second
# BatchSpanProcessor - deliberately deferred rather than half-built, because
# Langfuse is where these traces actually get read.
