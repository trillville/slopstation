"""Blind test: the agent lane's Sentry wiring - the DSN gate, the OTLP target
built from it, the span shapes we own, and the fail-soft rule that telemetry
must never cost a voice session. Run:
    .venv\\Scripts\\python tests\\test_sentry.py
"""
import json

import _bootstrap  # noqa: F401

import cglib
import events
from agent.telemetry import sentry

DSN = "https://abc123def456abc123def456abc12345@o4509876.ingest.us.sentry.io/1234567"


def main():

    # -- the gate --------------------------------------------------------------
    assert sentry.enabled({"sentryDsn": DSN})
    assert not sentry.enabled({})
    assert not sentry.enabled({"sentryDsn": ""})
    # Template junk reads as absent, same as every other keyed lane.
    assert not sentry.enabled({"sentryDsn": "https://...@o1.ingest.sentry.io/1"})
    print("  gate: one key, and placeholders read as absent")

    # -- trace-level attributes ------------------------------------------------
    tok = events.context(session="3b7e", turn="9f2c1a")
    a = sentry.span_attributes()
    assert a["session.id"] == "3b7e"
    # What Sentry groups a Conversation on - the same value, so a Conversation
    # and the JSONL lines around it are the same thing seen twice.
    assert a["gen_ai.conversation.id"] == "3b7e", a
    assert a["couch.turn"] == "9f2c1a"
    assert a["user.id"] and a["env"]
    assert sentry.conversation_id() == "3b7e"
    events.reset(tok)
    # With no context it still produces something usable, never None or "".
    assert sentry.conversation_id()
    assert "session.id" not in sentry.span_attributes()
    assert sentry.span_attributes(session="zz")["session.id"] == "zz"
    print("  attributes: session, conversation, turn, user")

    # OTel accepts str/bool/int/float and homogeneous sequences of those; a
    # dict or None is dropped at span creation, silently costing the field.
    for k, v in sentry.span_attributes(session="s", turn="t").items():
        ok = isinstance(v, (str, bool, int, float)) or (
            isinstance(v, list) and all(isinstance(i, str) for i in v))
        assert ok, f"{k}={v!r} is not an OTel-legal attribute value"
    print("  types: every attribute is OTel-legal")

    # -- the OTLP target -------------------------------------------------------
    # Built by the SDK's own DSN helper, so this is really asserting that the
    # helper still exists under those names - the one thing that would send
    # traces nowhere without any error.
    try:
        url, headers = sentry.otlp_target(DSN)
    except ImportError:
        print("  otlp: SKIPPED - sentry-sdk not installed in this venv")
    else:
        # Trailing slash included - Dsn.get_api_url always appends one.
        assert url == ("https://o4509876.ingest.us.sentry.io/api/1234567/"
                       "integration/otlp/v1/traces/"), url
        assert headers["X-Sentry-Auth"].startswith("Sentry "), headers
        assert "sentry_key=abc123def456abc123def456abc12345" \
            in headers["X-Sentry-Auth"]
        print(f"  otlp: {url}")

    # -- everything no-ops while tracing is off --------------------------------
    # This is also what keeps the --text REPL out of production Conversations:
    # voice_agent returns before setup() runs.
    assert sentry.is_on() is False
    assert sentry.tool_span("web_search", "hades reviews") is None
    with sentry.chat_span("anthropic", "claude-haiku-4-5") as span:
        assert span.span is None
        span.response(model="m", output="o", usage={"input": 1})   # no raise
    with sentry.session_trace() as trace_id:
        assert trace_id is None
        # And nothing was written into the ambient context.
        assert "trace" not in events.current()

    called = []
    decorated = sentry.agent("assistant")(lambda x: called.append(x) or "ret")
    assert decorated(1) == "ret" and called == [1]
    sentry.capture(RuntimeError("boom"))            # no SDK, no raise
    print("  off: spans, decorator, session trace and capture all no-op")

    # -- the usage map ---------------------------------------------------------
    # backends.py flattens its SDK's usage object onto these short keys; a
    # rename on either side silently drops token counts from the dashboard.
    assert set(sentry.USAGE_ATTR) == {"input", "output", "cache_read",
                                      "cache_write", "reasoning"}
    for key, attr in sentry.USAGE_ATTR.items():
        assert attr.startswith("gen_ai.usage."), (key, attr)
    print(f"  usage: {len(sentry.USAGE_ATTR)} keys -> gen_ai.usage.*")

    # -- fail-soft -------------------------------------------------------------
    log = cglib.CapturingLog("voice")

    # No DSN -> disabled quietly, NOT an error: the normal unconfigured state,
    # and the one that lets a deploy land before the K15 is touched.
    sentry._on = False
    assert sentry.setup({}, log) is False
    assert sentry.is_on() is False
    assert log.find("lane_disabled"), log.events()
    assert not log.find("sentry_setup_failed")
    assert not log.find("tracing_setup_failed")

    # voice_agent calls setup() BARE, before the wake loop, so anything that
    # escapes it is an agent that will not start. A DSN present and the SDK
    # exploding must be an error event and a False, never a raise.
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))  # noqa: E731
    log2 = cglib.CapturingLog("voice")
    real_init, sentry._init = sentry._init, boom
    try:
        assert sentry.setup({"sentryDsn": DSN}, log2) is False
    finally:
        sentry._init = real_init
    assert log2.find("sentry_setup_failed"), log2.events()
    assert sentry.is_on() is False

    # The SDK merely declining (no sentry-sdk in the venv) stops there rather
    # than half-wiring tracing on top of it.
    log3 = cglib.CapturingLog("voice")
    sentry._init = lambda *a, **k: False
    try:
        assert sentry.setup({"sentryDsn": DSN}, log3) is False
    finally:
        sentry._init = real_init
    assert not log3.find("tracing_setup_failed"), log3.events()
    print("  fail-soft: no DSN is a quiet lane_disabled; a throwing init is "
          "an event and a False, never a raise")

    # -- the tool span shape ---------------------------------------------------
    # Sentry's execute_tool contract, asserted on the constant rather than on a
    # live span: op, operation name and the argument key are what the Tool
    # Errors and Tool Calls widgets read.
    src = (_bootstrap.AGENT / "telemetry" / "sentry.py").read_text(
        encoding="utf-8")
    for required in ('"sentry.op": "gen_ai.execute_tool"',
                     '"gen_ai.operation.name": "execute_tool"',
                     '"gen_ai.tool.call.arguments"',
                     '"gen_ai.tool.call.result"',
                     '"sentry.op": "gen_ai.invoke_agent"',
                     '"sentry.op": "gen_ai.chat"'):
        assert required in src, required
    print("  shapes: chat, execute_tool and invoke_agent all name sentry.op")

    # -- the response recorder -------------------------------------------------
    class FakeSpan:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v

    fs = FakeSpan()
    sentry._Chat(fs).response(model="claude-haiku-4-5", output="hello",
                              usage={"input": 900, "output": 20,
                                     "cache_read": 800, "bogus": 1})
    assert fs.attrs["gen_ai.response.model"] == "claude-haiku-4-5"
    assert fs.attrs["gen_ai.usage.input_tokens"] == 900
    assert fs.attrs["gen_ai.usage.cache_read.input_tokens"] == 800
    assert "bogus" not in json.dumps(fs.attrs), fs.attrs
    msgs = json.loads(fs.attrs["gen_ai.output.messages"])
    assert msgs[0]["parts"][0]["content"] == "hello"
    print("  recorder: model, output and known usage keys only")

    print("OK - sentry: dsn gate, otlp target, attributes, span shapes, "
          "fail-soft")


if __name__ == "__main__":
    main()
