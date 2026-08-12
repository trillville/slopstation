"""Query the couch system's logs in Grafana Cloud Loki, from the terminal.

    python query.py '{service="k15", level="error"}' --since 24h
    python query.py '{service=~"k15|gamepc"} | json | turn="9f2c1a"'
    python query.py 'sum(count_over_time({service="k15", lane="voice"} | json | event="wake" [1h]))' --since 7d

Stdlib only, so it runs from any checkout without a venv.

CREDENTIALS (read-only; the shipper's write token cannot query):
    k15/secrets.json   ->  "grafanaLokiUser", "grafanaReadToken"
    or env             ->  GC_LOKI_USER, GC_LOKI_READ_TOKEN
"""
import argparse
import base64
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://logs-prod-021.grafana.net"
REPO = pathlib.Path(__file__).resolve().parents[3]

# Rendered first, in this order - the rest of a line's fields follow as k=v.
LEAD = ("event", "turn", "session", "dur_ms", "err")


def credentials():
    import os
    user = os.environ.get("GC_LOKI_USER")
    token = os.environ.get("GC_LOKI_READ_TOKEN")
    url = os.environ.get("GC_LOKI_URL", DEFAULT_URL)
    try:
        s = json.loads((REPO / "k15" / "secrets.json").read_text(encoding="utf-8-sig"))
        user = user or s.get("grafanaLokiUser")
        token = token or s.get("grafanaReadToken")
        url = s.get("grafanaLokiUrl") or url
    except (OSError, ValueError):
        pass
    if not user or not token or "..." in str(token):
        sys.exit(
            "No Loki read credentials.\n"
            "  Add to k15/secrets.json:  \"grafanaLokiUser\": \"1730320\",\n"
            "                            \"grafanaReadToken\": \"glc_...\"\n"
            "  The token needs logs:READ - the shipper's write token will 401 with\n"
            "  'invalid scope requested'. Make one under the stack-*-hl-read policy\n"
            "  at grafana.com -> Cloud portal -> Security -> Access Policies.")
    return user, token, url.rstrip("/")


def seconds(spec):
    m = re.fullmatch(r"(\d+)([smhdw])", spec.strip())
    if not m:
        sys.exit(f"bad --since {spec!r}; use 30m, 6h, 7d")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def fetch(logql, since, limit):
    user, token, base = credentials()
    end = time.time()
    q = urllib.parse.urlencode({
        "query": logql,
        "start": int((end - since) * 1e9),
        "end": int(end * 1e9),
        "limit": limit,
        "direction": "backward",
    })
    req = urllib.request.Request(f"{base}/loki/api/v1/query_range?{q}")
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"{user}:{token}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        hint = {
            401: "  -> the token lacks logs:read (a write token gives exactly this)",
            400: "  -> LogQL syntax. Fields need `| json |` first; labels do not.",
        }.get(e.code, "")
        sys.exit(f"HTTP {e.code} from Loki\n{body}\n{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {base}: {e.reason}")


def render_line(stream, ts_ns, line):
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts_ns) / 1e9))
    where = f"{stream.get('service','?')}/{stream.get('lane','?')}"
    level = stream.get("level", "info")
    mark = {"error": "ERROR ", "warn": "WARN  "}.get(level, "")
    try:
        d = json.loads(line)
    except ValueError:
        return f"{when}  {where:20} {mark}{line.rstrip()}"
    skip = {"ts", "level", "env", "service", "lane", "host"}
    parts = [str(d[k]) if k == "event" else f"{k}={d[k]}"
             for k in LEAD if k in d]
    parts += [f"{k}={v}" for k, v in d.items()
              if k not in skip and k not in LEAD]
    return f"{when}  {where:20} {mark}{' '.join(parts)}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logql")
    ap.add_argument("--since", default="6h", help="30m, 6h, 7d (default 6h)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--json", action="store_true", help="raw Loki response")
    a = ap.parse_args()

    data = fetch(a.logql, seconds(a.since), a.limit)
    if a.json:
        print(json.dumps(data, indent=1))
        return
    result = data.get("data", {}).get("result", [])
    if not result:
        print(f"(no results in the last {a.since})")
        return

    if data["data"].get("resultType") == "matrix":       # metric query
        for series in result:
            name = series.get("metric") or "value"
            pts = series.get("values", [])
            if pts:
                latest = pts[-1]
                print(f"{name}  latest={latest[1]}  points={len(pts)}")
        return

    rows = [(v[0], render_line(s["stream"], v[0], v[1]))
            for s in result for v in s["values"]]
    rows.sort()
    for _, text in rows:
        print(text)
    print(f"\n{len(rows)} line(s) in the last {a.since}")


if __name__ == "__main__":
    main()
