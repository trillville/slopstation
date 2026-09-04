"""Test Sentry configuration and trace shapes."""

import json

import pytest

from helpers import CapturingLog
from slopstation import events
from slopstation.agent.telemetry import sentry

DSN = "https://abc123def456abc123def456abc12345@o4509876.ingest.us.sentry.io/1234567"


# -- trace-level attributes ------------------------------------------------


def test_span_attributes_carry_the_ambient_ids():
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


# -- the OTLP target -------------------------------------------------------


def test_otlp_target_comes_from_the_sdk_dsn_helper():
    # Built by the SDK's own DSN helper, so this is really asserting that the
    # helper still exists under those names - the one thing that would send
    # traces nowhere without any error.
    url, headers = sentry.otlp_target(DSN)
    # Trailing slash included - Dsn.get_api_url always appends one.
    assert url == (
        "https://o4509876.ingest.us.sentry.io/api/1234567/integration/otlp/v1/traces/"
    ), url
    assert headers["X-Sentry-Auth"].startswith("Sentry "), headers
    assert "sentry_key=abc123def456abc123def456abc12345" in headers["X-Sentry-Auth"]


# -- everything no-ops while tracing is off --------------------------------


def test_everything_no_ops_while_tracing_is_off():
    # This is also what keeps the --text REPL out of production Conversations:
    # The voice service returns before setup() runs.
    assert sentry.is_on() is False
    assert sentry.tool_span("web_search", "hades reviews") is None
    with sentry.chat_span("anthropic", "claude-haiku-4-5") as span:
        assert span.span is None
        span.response(model="m", output="o", usage={"input": 1})  # no raise
    with sentry.session_trace() as trace_id:
        assert trace_id is None
        # And nothing was written into the ambient context.
        assert "trace" not in events.current()

    called = []
    decorated = sentry.agent("assistant")(lambda x: called.append(x) or "ret")
    assert decorated(1) == "ret" and called == [1]
    sentry.capture(RuntimeError("boom"))  # no SDK, no raise


# -- Error handling --------------------------------------------------------


@pytest.fixture
def _tracing_off(monkeypatch):
    monkeypatch.setattr(sentry, "_on", False)


def _boom(*a, **k):
    raise RuntimeError("no network")


def test_setup_without_a_dsn_is_disabled_quietly(_tracing_off):
    # No DSN -> disabled quietly, NOT an error: the normal unconfigured state,
    # and the one that lets a deploy land before the K15 is touched. Template
    # junk reads as absent, same as every other keyed lane.
    log = CapturingLog("voice")
    for cfg in (
        {},
        {"sentryDsn": ""},
        {"sentryDsn": "https://...@o1.ingest.sentry.io/1"},
    ):
        assert sentry.setup(cfg, log) is False, cfg
    assert sentry.is_on() is False
    assert len(log.find("lane_disabled")) == 3, log.events()
    assert not log.find("sentry_setup_failed")
    assert not log.find("tracing_setup_failed")


def test_an_exploding_sdk_is_an_error_event_and_a_false(_tracing_off, monkeypatch):
    # The voice service calls setup() before the wake loop, so anything that
    # escapes it is an agent that will not start. A DSN present and the SDK
    # exploding must be an error event and a False, never a raise.
    log = CapturingLog("voice")
    monkeypatch.setattr(sentry, "_init", _boom)
    assert sentry.setup({"sentryDsn": DSN}, log) is False
    assert log.find("sentry_setup_failed"), log.events()
    assert sentry.is_on() is False


def test_a_declining_sdk_stops_before_tracing(_tracing_off, monkeypatch):
    # The SDK merely declining (no sentry-sdk in the venv) stops there rather
    # than half-wiring tracing on top of it.
    log = CapturingLog("voice")
    monkeypatch.setattr(sentry, "_init", lambda *a, **k: False)
    assert sentry.setup({"sentryDsn": DSN}, log) is False
    assert not log.find("tracing_setup_failed"), log.events()


# -- the response recorder -------------------------------------------------


class _FakeSpan(dict):
    """The attributes set on it, as a dict."""

    def set_attribute(self, k, v):
        self[k] = v


def test_response_recorder():
    fs = _FakeSpan()
    sentry._Chat(fs).response(
        model="claude-haiku-4-5",
        output="hello",
        usage={"input": 900, "output": 20, "cache_read": 800, "bogus": 1},
    )
    assert fs["gen_ai.response.model"] == "claude-haiku-4-5"
    assert fs["gen_ai.usage.input_tokens"] == 900
    assert fs["gen_ai.usage.cache_read.input_tokens"] == 800
    assert "bogus" not in json.dumps(fs), fs
    msgs = json.loads(fs["gen_ai.output.messages"])
    assert msgs[0]["parts"][0]["content"] == "hello"
