"""Blind test: agent tracing wiring - the auth header, endpoint and
trace-level attributes Sentry renders from, and the fail-soft rule that
tracing must never cost a voice session. Run:
    .venv\\Scripts\\python tests\\test_tracing.py
"""
import _bootstrap  # noqa: F401

import cglib
import events
from agent.telemetry import tracing

REAL = {"sentry": {"orgId": "4509876",
                   "projectId": "1234567",
                   "publicKey": "a" * 32}}


def main():

    # -- the config gate -------------------------------------------------------
    assert tracing.enabled(REAL)
    assert not tracing.enabled({})
    assert not tracing.enabled({"sentry": {}})
    # Every id is required: a half-filled block is not "configured".
    assert not tracing.enabled({"sentry": {"orgId": "4509876"}})
    assert not tracing.enabled({"sentry": {"orgId": "4509876",
                                           "projectId": "1234567"}})
    # Template junk reads as absent, same as every other keyed lane.
    assert not tracing.enabled({"sentry": {"orgId": "4509876",
                                           "projectId": "1234567",
                                           "publicKey": "PLACEHOLDER..."}})
    print("  gate: org, project and a real key required; placeholders absent")

    # -- auth header -----------------------------------------------------------
    h = tracing.auth_header("abc123")
    assert h == "sentry sentry_key=abc123", h
    print("  auth: sentry sentry_key=<public key>")

    # -- endpoint --------------------------------------------------------------
    u = tracing.endpoint(REAL)
    assert u == ("https://o4509876.ingest.sentry.io"
                 "/api/1234567/integration/otlp/v1/traces"), u
    # A region-scoped DSN host is used verbatim, not rebuilt from orgId.
    regional = {"sentry": dict(REAL["sentry"],
                               ingestHost="o4509876.ingest.us.sentry.io")}
    assert tracing.endpoint(regional) == (
        "https://o4509876.ingest.us.sentry.io"
        "/api/1234567/integration/otlp/v1/traces")
    # Unconfigured returns "", never None: the caller concatenates it.
    assert tracing.endpoint({}) == ""
    print(f"  endpoint: {u}")

    # -- trace-level attributes ------------------------------------------------
    tok = events.context(session="3b7e", turn="9f2c1a")
    a = tracing.span_attributes()
    assert a["session.id"] == "3b7e"
    # What groups turns into one conversation in Sentry.
    assert a["gen_ai.conversation.id"] == "3b7e"
    assert a["couch.turn"] == "9f2c1a"
    assert a["user.id"] and a["env"]
    # Conversation id is the session id, so a trace and its log lines join.
    assert tracing.conversation_id() == "3b7e"
    events.reset(tok)
    # With no context it still produces something usable, never None/"".
    assert tracing.conversation_id()
    assert "session.id" not in tracing.span_attributes()
    assert tracing.span_attributes(session="zz")["session.id"] == "zz"
    print("  attributes: session, conversation, user, env; conversation == session")

    # -- OTel attribute types --------------------------------------------------
    # OTel accepts str/bool/int/float and homogeneous sequences of those; a dict
    # or None is dropped at span creation, silently costing the field. Sentry
    # narrows that further - it cannot search a sequence - so these are scalar.
    for k, v in tracing.span_attributes(session="s", turn="t").items():
        assert isinstance(v, (str, bool, int, float)), \
            f"{k}={v!r} is not a scalar Sentry can search"
    print("  types: every attribute is a searchable scalar")

    # -- fail-soft --------------------------------------------------------------
    log = cglib.CapturingLog("voice")

    # No block -> disabled quietly, NOT an error: the normal unconfigured
    # state, and the one that lets the deploy land before the K15 is touched.
    tracing._on = False
    assert tracing.setup({}, log) is False
    assert tracing.is_on() is False
    assert log.find("lane_disabled"), log.events()
    assert not log.find("tracing_setup_failed")

    # Configured but the exporter explodes -> error, still False, no raise.
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))  # noqa: E731
    real_exporter, tracing._exporter = tracing._exporter, boom
    try:
        assert tracing.setup(REAL, log) is False
        assert tracing.is_on() is False
    finally:
        tracing._exporter = real_exporter
    assert log.find("tracing_setup_failed"), log.events()
    print("  fail-soft: missing config and a throwing exporter both return False")

    # -- the real exporter builds ----------------------------------------------
    # Only meaningful once the venv has the OTel pins; skipped without them,
    # since that is the state setup() must survive.
    try:
        exp = tracing._exporter(REAL)
        assert exp is not None
        assert "http" in type(exp).__module__, type(exp).__module__
        print(f"  exporter: {type(exp).__module__.split('.')[-2]} (HTTP, not gRPC)")
    except ImportError:
        print("  exporter: SKIPPED - opentelemetry not installed in this venv")

    print("OK - tracing: gate, auth, endpoint, attributes, types, fail-soft")


if __name__ == "__main__":
    main()
