"""Query the couch system's logs in Grafana Cloud Loki, from the terminal.

    python query.py --level error --since 24h
    python query.py --turn 9f2c1a --since 24h          # both machines, one intent
    python query.py --lane voice --event gate_miss
    python query.py 'sum(count_over_time({service="k15"} | json | event="wake" [1h]))' --since 7d

Prefer the flags. PowerShell strips double quotes when passing a raw LogQL
string to a native process, which Loki reports as an error about a query you
never typed; the flags compose it here instead.

Stdlib only, so it runs from any checkout without a venv.

CREDENTIALS (read-only; the shipper's write token cannot query):
    k15/secrets.json   ->  "grafanaLokiUser", "grafanaReadToken"
    or env             ->  GC_LOKI_USER, GC_LOKI_READ_TOKEN
On a worktree (<repo>/.claude/worktrees/<name>) the gitignored secrets file
does not exist locally; the enclosing checkout's k15/secrets.json is read
automatically.
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


def repo_roots():
    """This checkout first; then, when it is a worktree under
    <repo>/.claude/worktrees/<name>, the enclosing checkout - gitignored
    files (secrets.json) exist only where a human put them, which is the
    main checkout, not the worktree an agent session happens to run in."""
    roots = [REPO]
    if REPO.parent.name == "worktrees" and REPO.parent.parent.name == ".claude":
        roots.append(REPO.parent.parent.parent)
    return roots

# Rendered first, in this order - the rest of a line's fields follow as k=v.
LEAD = ("event", "turn", "session", "dur_ms", "err")


def credentials():
    import os
    user = os.environ.get("GC_LOKI_USER")
    token = os.environ.get("GC_LOKI_READ_TOKEN")
    url = os.environ.get("GC_LOKI_URL", DEFAULT_URL)
    for root in repo_roots():
        try:
            s = json.loads((root / "k15" / "secrets.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        user = user or s.get("grafanaLokiUser")
        token = token or s.get("grafanaReadToken")
        url = s.get("grafanaLokiUrl") or url
        break
    if not user or not token or "..." in str(token):
        sys.exit(
            "No Loki read credentials (worktrees fall back to the enclosing\n"
            "checkout's k15/secrets.json - NEITHER had them).\n"
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


def build(a):
    """Compose LogQL from flags, so the common questions need no quoting at
    all. PowerShell strips the double quotes out of a raw '{service="k15"}'
    when handing it to a native process, and Loki answers with
    'unexpected IDENTIFIER, expecting STRING' - a confusing error about a
    query the user never actually typed. Flags sidestep it entirely."""
    labels = {}
    if a.service:
        labels["service"] = a.service
    if a.lane:
        labels["lane"] = a.lane
    if a.level:
        labels["level"] = a.level
    labels["env"] = a.env
    # A turn or session spans both machines by definition, so widen unless
    # the caller pinned a service.
    if (a.turn or a.session) and "service" not in labels:
        labels["service"] = "k15|gamepc"

    sel = ", ".join(
        f'{k}=~"{v}"' if "|" in v else f'{k}="{v}"' for k, v in labels.items())
    q = "{" + sel + "}"
    fields = [(k, getattr(a, k)) for k in ("event", "turn", "session")
              if getattr(a, k)]
    if fields:
        q += " | json | " + " | ".join(f'{k}="{v}"' for k, v in fields)
    if a.contains:
        q += f' |= "{a.contains}"'
    return q


def looks_mangled(q):
    """A label value that lost its quotes - the PowerShell footgun."""
    return bool(re.search(r'[{,]\s*\w+\s*=~?\s*[A-Za-z0-9_]', q))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logql", nargs="?", help="raw LogQL (or use the flags below)")
    ap.add_argument("--service", help="k15 | gamepc")
    ap.add_argument("--lane", help="voice | launch | listener | supervisor | ...")
    ap.add_argument("--level", help="error | warn | info")
    ap.add_argument("--event", help="host_ready, launch_failed, gate_miss, ...")
    ap.add_argument("--turn", help="follow one intent across BOTH machines")
    ap.add_argument("--session", help="one voice conversation")
    ap.add_argument("--contains", help="plain substring match")
    ap.add_argument("--env", default="prod", help="prod (default) | test")
    ap.add_argument("--since", default="6h", help="30m, 6h, 7d (default 6h)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--json", action="store_true", help="raw Loki response")
    a = ap.parse_args()

    if a.logql:
        if looks_mangled(a.logql):
            sys.exit(
                f"That query has an unquoted label value:\n  {a.logql}\n\n"
                "PowerShell strips double quotes when passing to a native\n"
                "process. Either use the flags (no quoting needed):\n"
                "  --service k15 --level error --since 24h\n"
                "or escape them:  '{service=\\\"k15\\\"}'")
        logql = a.logql
    else:
        logql = build(a)
        print(f"# {logql}\n")

    data = fetch(logql, seconds(a.since), a.limit)
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
