"""Read agent traces from Langfuse, from the terminal.

    python query.py conversations --since 24h        # one row per voice session
    python query.py trace <traceId>                  # the full tree, timed
    python query.py errors --since 24h               # observations at level ERROR
    python query.py session <sessionId>              # everything in one session

Stdlib only, so it runs from any checkout without a venv.

CREDENTIALS - the SAME pair the voice agent exports with; no extra token:
    k15/secrets.json  ->  "langfusePublicKey", "langfuseSecretKey"
    or env            ->  LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

NOTE ON THE API: Langfuse has no /traces list endpoint. Traces are read
through /api/public/v2/observations with isRootObservation=true - which is
exactly what the UI's "Is Root Observation" filter does. One root observation
== one conversation.
"""
import argparse
import base64
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_HOST = "https://us.cloud.langfuse.com"
REPO = pathlib.Path(__file__).resolve().parents[3]


def credentials():
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST")
    try:
        s = json.loads((REPO / "k15" / "secrets.json").read_text(encoding="utf-8-sig"))
        pk = pk or s.get("langfusePublicKey")
        sk = sk or s.get("langfuseSecretKey")
    except (OSError, ValueError):
        pass
    if not host:
        try:
            cfg = json.loads((REPO / "k15" / "config.json").read_text(encoding="utf-8"))
            host = cfg.get("voice", {}).get("langfuseHost")
        except (OSError, ValueError):
            pass
    if not pk or not sk or "..." in str(sk):
        sys.exit(
            "No Langfuse keys.\n"
            "  Add to k15/secrets.json:  \"langfusePublicKey\": \"pk-lf-...\",\n"
            "                            \"langfuseSecretKey\": \"sk-lf-...\"\n"
            "  Same pair the voice agent uses - Langfuse project settings > API keys.")
    return pk, sk, (host or DEFAULT_HOST).rstrip("/")


def seconds(spec):
    m = re.fullmatch(r"(\d+)([smhdw])", spec.strip())
    if not m:
        sys.exit(f"bad --since {spec!r}; use 30m, 6h, 7d")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600,
                              "d": 86400, "w": 604800}[m.group(2)]


def get(path, **params):
    pk, sk, host = credentials()
    params = {k: v for k, v in params.items() if v is not None}
    url = f"{host}/api/public/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"{pk}:{sk}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        hint = {
            401: "  -> wrong keys, or the wrong region host (EU vs US)",
            404: f"  -> no such endpoint at {url.split('?')[0]}",
        }.get(e.code, "")
        sys.exit(f"HTTP {e.code} from Langfuse\n{body}\n{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach Langfuse: {e.reason}")


def iso_ago(secs):
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def local(ts):
    """Langfuse timestamps are ISO-8601 UTC; show them in local time, which is
    what the couch.log lines the user is comparing against are stamped in."""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t.astimezone().strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts)[:19]


def _f(o, *names, default=None):
    for n in names:
        if isinstance(o, dict) and o.get(n) is not None:
            return o[n]
    return default


def cmd_conversations(a):
    rows = _f(get("v2/observations", isRootObservation="true", limit=a.limit,
                  fromStartTime=iso_ago(seconds(a.since))),
              "data", default=[])
    if not rows:
        print(f"(no conversations in the last {a.since})")
        return
    print(f"{'when':15} {'trace id':34} {'session':9} {'env':6} {'lat':>7}  name")
    for o in rows:
        print(f"{local(_f(o,'startTime','timestamp')):15} "
              f"{str(_f(o,'traceId','id','')):34} "
              f"{str(_f(o,'sessionId',default='-'))[:9]:9} "
              f"{str(_f(o,'environment',default='-'))[:6]:6} "
              f"{_fmt_lat(o):>7}  {_f(o,'name',default='')}")
    # No cost column, deliberately: a ROOT observation's cost fields are null.
    # Cost lives on the GENERATION spans and the UI aggregates them, so showing
    # it here would cost one extra API call per row. `trace` sums it instead,
    # from spans it has already fetched.
    print(f"\n{len(rows)} conversation(s). Cost and tokens: trace <trace id>")


def _fmt_lat(o):
    v = _f(o, "latency", "latencySeconds")
    return f"{v:.1f}s" if isinstance(v, (int, float)) else "-"


def _fmt_cost(o):
    v = _f(o, "calculatedTotalCost", "totalPrice", "totalCost")
    # Blank, not $0.000000, for the spans that genuinely have no cost (stt,
    # tts, turn, conversation). A column of zeros reads as "cost tracking is
    # broken" when it actually means "this span is not a model call".
    return f"${v:.6f}" if isinstance(v, (int, float)) and v else ""


def cmd_trace(a):
    # 100 is Langfuse's hard maximum; asking for more is a 400, not a clamp.
    rows = _f(get("observations", traceId=a.trace_id, limit=100),
              "data", default=[])
    if not rows:
        sys.exit(f"no observations for trace {a.trace_id}")
    rows.sort(key=lambda o: str(_f(o, "startTime", "timestamp", default="")))
    by_parent = {}
    for o in rows:
        by_parent.setdefault(_f(o, "parentObservationId"), []).append(o)

    def walk(parent, depth):
        for o in by_parent.get(parent, []):
            pad = "  " * depth
            bits = [f"{pad}{_f(o,'name',default='?'):<14}",
                    f"{_fmt_lat(o):>7}", f"{_fmt_cost(o):>10}"]
            usage = _f(o, "usage", "usageDetails", default={}) or {}
            tin = _f(usage, "input", "promptTokens", "inputTokens")
            tout = _f(usage, "output", "completionTokens", "outputTokens")
            if tin or tout:
                bits.append(f"  {tin or 0}->{tout or 0} tok")
            if _f(o, "model"):
                bits.append(f"  {_f(o,'model')}")
            print(" ".join(bits))
            for field in ("input", "output"):
                val = _f(o, field)
                if val and a.io:
                    text = json.dumps(val) if not isinstance(val, str) else val
                    print(f"{pad}    {field}: {text[:300]}")
            walk(_f(o, "id"), depth + 1)

    root = by_parent.get(None, [{}])[0]
    # sessionId is a v2-only field, and it is the join key back to the Loki
    # logs - worth one extra call rather than printing a dash for the most
    # useful identifier on the page.
    session = _f(root, "sessionId", default=None)
    if not session:
        peek = _f(get("v2/observations", traceId=a.trace_id,
                      isRootObservation="true", limit=1), "data", default=[])
        session = _f(peek[0], "sessionId", default="-") if peek else "-"
    root = dict(root, sessionId=session)
    cost = sum(o.get("calculatedTotalCost") or 0 for o in rows)
    tin = sum((o.get("usage") or {}).get("input") or 0 for o in rows)
    tout = sum((o.get("usage") or {}).get("output") or 0 for o in rows)
    cached = sum((o.get("usageDetails") or {}).get("input_cached_tokens") or 0
                 for o in rows)
    print(f"trace {a.trace_id}\n"
          f"  session={_f(root,'sessionId',default='-')}  "
          f"env={_f(root,'environment',default='-')}  "
          f"started {local(_f(root,'startTime','timestamp'))}  "
          f"{_fmt_lat(root)}")
    # Cached share is the number that explains the cost: the catalog sits in
    # every prompt, and caching is the only reason that is affordable.
    share = f" ({cached / tin:.0%} cached)" if tin else ""
    print(f"  ${cost:.6f}   {tin:,} in{share} -> {tout:,} out   {len(rows)} spans\n")
    walk(None, 0)
    if len(rows) == 100:
        print("\n! 100 spans returned - Langfuse's page maximum, so this trace "
              "may be truncated")
    print("\n(pass --io to print prompts and completions)")


def cmd_errors(a):
    rows = _f(get("observations", level="ERROR", limit=a.limit,
                  fromStartTime=iso_ago(seconds(a.since))),
              "data", default=[])
    if not rows:
        print(f"(no ERROR observations in the last {a.since}) - agent lane clean")
        return
    for o in rows:
        print(f"{local(_f(o,'startTime','timestamp'))}  {_f(o,'name',default='?'):<14} "
              f"{str(_f(o,'statusMessage',default=''))[:120]}")
        print(f"     trace {_f(o,'traceId',default='-')}")
    print(f"\n{len(rows)} error observation(s)")


def cmd_session(a):
    rows = _f(get("v2/observations", sessionId=a.session_id, limit=a.limit),
              "data", default=[])
    if not rows:
        sys.exit(f"nothing for session {a.session_id}")
    traces = {}
    for o in rows:
        traces.setdefault(_f(o, "traceId"), []).append(o)
    print(f"session {a.session_id}: {len(rows)} observations "
          f"across {len(traces)} trace(s)")
    for tid, obs in traces.items():
        print(f"  {tid}  {len(obs)} spans  "
              f"first {local(min(str(_f(o,'startTime','timestamp','')) for o in obs))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("conversations", help="one row per voice session")
    p.add_argument("--since", default="24h")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_conversations)

    p = sub.add_parser("trace", help="the full tree for one trace")
    p.add_argument("trace_id")
    p.add_argument("--io", action="store_true", help="print prompts/completions")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("errors", help="observations at level ERROR")
    p.add_argument("--since", default="24h")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("session", help="everything under one session id")
    p.add_argument("session_id")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_session)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
