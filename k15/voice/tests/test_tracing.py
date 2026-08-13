"""Blind test: agent tracing wiring. Run:
    .venv\\Scripts\\python tests\\test_tracing.py

Everything Langfuse actually renders from is asserted here - the auth header,
the endpoint, and the trace-level attributes - because none of it is visible
until traces are already flowing wrongly. The rest of the file is the rule
that matters more than any of it: tracing must never be able to cost a voice
session, so every entry point is driven into failure and expected to shrug.
"""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cglib
import events
import tracing

REAL = {"langfusePublicKey": "pk-lf-" + "a" * 20,
        "langfuseSecretKey": "sk-lf-" + "b" * 20}

def main():

    # -- the key gate ----------------------------------------------------------
    assert tracing.enabled(REAL)
    assert not tracing.enabled({})
    assert not tracing.enabled({"langfusePublicKey": REAL["langfusePublicKey"]})
    # Template junk reads as absent, same rule as every other keyed lane.
    assert not tracing.enabled({"langfusePublicKey": "pk-lf-...",
                                "langfuseSecretKey": "sk-lf-..."})
    print("  gate: both keys required, placeholders read as absent")

    # -- auth header -----------------------------------------------------------
    h = tracing.auth_header("pk-lf-x", "sk-lf-y")
    assert h.startswith("Basic "), h
    assert base64.b64decode(h.split(" ", 1)[1]).decode() == "pk-lf-x:sk-lf-y"
    # A literal space, NOT %20 - the escape belongs to the env-var format.
    assert "%20" not in h, "the env-var escape leaked into the header value"
    print("  auth: Basic <b64(pk:sk)>, literal space")

    # -- endpoint --------------------------------------------------------------
    us = tracing.endpoint({"voice": {"langfuseHost": "https://us.cloud.langfuse.com"}})
    assert us == "https://us.cloud.langfuse.com/api/public/otel/v1/traces", us
    assert tracing.endpoint({"voice": {"langfuseHost": "https://x.dev/"}}) \
        == "https://x.dev/api/public/otel/v1/traces"
    # Missing/empty falls back rather than building a broken URL.
    assert tracing.endpoint({}) == tracing.DEFAULT_HOST + tracing.TRACES_PATH
    assert tracing.endpoint({"voice": {"langfuseHost": ""}}).startswith("https://")
    print(f"  endpoint: {us}")

    # -- trace-level attributes ------------------------------------------------
    tok = events.context(session="3b7e", turn="9f2c1a")
    a = tracing.span_attributes()
    # Without a name every trace in the list is "conversation" or a UUID.
    assert a["langfuse.trace.name"], a
    assert a["langfuse.session.id"] == "3b7e" and a["session.id"] == "3b7e"
    assert a["couch.turn"] == "9f2c1a"
    assert a["langfuse.user.id"] and a["user.id"]
    assert isinstance(a["langfuse.trace.tags"], list)
    # Conversation id is the session id, so a trace and its log lines join.
    assert tracing.conversation_id() == "3b7e"
    events.reset(tok)
    # With no context it still produces something usable, never None/"".
    assert tracing.conversation_id()
    assert "langfuse.session.id" not in tracing.span_attributes()
    assert tracing.span_attributes(session="zz")["langfuse.session.id"] == "zz"
    print("  attributes: name, session, user, tags; conversation id == session")

    # -- OTel attribute types --------------------------------------------------
    # OTel accepts str/bool/int/float and homogeneous sequences of those. A
    # dict or None is dropped at span creation, silently costing the field.
    for k, v in tracing.span_attributes(session="s", turn="t").items():
        ok = isinstance(v, (str, bool, int, float)) or (
            isinstance(v, list) and all(isinstance(i, str) for i in v))
        assert ok, f"{k}={v!r} is not an OTel-legal attribute value"
    print("  types: every attribute is OTel-legal")

    # -- fail-soft: the rule that outranks all of the above --------------------
    log = cglib.CapturingLog("voice")

    # No keys -> disabled quietly, NOT an error: the normal unconfigured state.
    tracing._on = False
    assert tracing.setup({}, {}, log) is False
    assert tracing.is_on() is False
    assert log.find("lane_disabled"), log.events()
    assert not log.find("tracing_setup_failed")

    # Keys present but the exporter explodes -> error, still False, no raise.
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))  # noqa: E731
    real_exporter, tracing._exporter = tracing._exporter, boom

    # -- the real exporter builds ----------------------------------------------
    try:
        assert tracing.setup({}, REAL, log) is False
        assert tracing.is_on() is False
    finally:
        tracing._exporter = real_exporter
    assert log.find("tracing_setup_failed"), log.events()
    print("  fail-soft: missing keys and a throwing exporter both return False")

    # Only meaningful once the venv has the OTel pins; skipped rather than
    # failed without them, because that is the state setup() must survive.
    try:
        exp = tracing._exporter(
            {"voice": {"langfuseHost": "https://us.cloud.langfuse.com"}}, REAL)
        assert exp is not None
        assert "http" in type(exp).__module__, type(exp).__module__
        print(f"  exporter: {type(exp).__module__.split('.')[-2]} (HTTP, not gRPC)")
    except ImportError:
        print("  exporter: SKIPPED - opentelemetry not installed in this venv")

    # -- background jobs: same trace, minutes later, another thread -----------
    # The point of the whole mechanism: a job queued during a conversation must
    # report UNDER that conversation, not as an orphan trace. Asserted on real
    # trace ids from an in-memory exporter, never on how the UI looks.
    tracing._on = False
    assert tracing.carrier() is None            # off -> nothing to propagate
    with tracing.job_span("j1", "task", None) as j:
        j.step("WebSearch", "q", "r")           # must be silent no-ops
        j.finish("s", "d", {"cost_usd": 1})
    try:
        with tracing.job_span("j1", "task", None):
            raise ValueError("body")
    except ValueError:
        pass                                    # never SUPPRESSES the body
    else:
        raise AssertionError("job_span swallowed an exception from its body")
    print("  jobs: tracing off -> null helper, and the body still raises")

    try:
        from opentelemetry import trace as otel
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter)
    except ImportError:
        print("  jobs: SKIPPED parenting - opentelemetry not installed")
    else:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        otel.set_tracer_provider(provider)
        tracing._on = True
        tracer = otel.get_tracer("test")
        with tracer.start_as_current_span("conversation") as conv:
            parent_trace = conv.get_span_context().trace_id
            carrier = tracing.carrier()          # frozen while conv is active
        assert carrier and "traceparent" in carrier, carrier
        # The conversation span has now ENDED - this is the real situation.
        with tracing.job_span("j2", "research couch co-op", carrier,
                              session="36d22d", provider="claude") as j:
            j.step("WebSearch", "couch co-op 2026", "ten results")
            j.finish("Three picks.", "The long form.",
                     {"cost_usd": 0.073, "turns": 4, "input_tokens": 2})
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert "background task" in spans and "tool: WebSearch" in spans
        job, tool = spans["background task"], spans["tool: WebSearch"]
        assert job.context.trace_id == parent_trace, \
            "the job landed in its own trace - the whole mechanism is the join"
        assert tool.context.trace_id == parent_trace
        assert tool.parent.span_id == job.context.span_id   # nested, not flat
        assert job.attributes["langfuse.session.id"] == "36d22d"
        assert job.attributes["couch.job.cost_usd"] == 0.073
        assert job.attributes["gen_ai.usage.input_tokens"] == 2
        assert "Three picks." in job.attributes["langfuse.observation.output"]
        assert tool.attributes["gen_ai.tool.name"] == "WebSearch"
        tracing._on = False
        print("  jobs: job + tool spans land in the CONVERSATION's trace, "
              "nested, after it closed")

    print("OK - tracing: gate, auth, endpoint, attributes, types, fail-soft")

if __name__ == "__main__":
    main()
