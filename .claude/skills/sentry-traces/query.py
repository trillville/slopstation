"""Read agent traces from Sentry, from the terminal.

    python query.py conversations --since 24h     # one row per voice session
    python query.py session c32ec7                # every span in one session
    python query.py trace <traceId>               # one trace, oldest first
    python query.py trace <traceId> --io          # + prompts and completions
    python query.py tools --since 7d              # tool calls, by name

Stdlib only, so it runs from any checkout without a venv.

CREDENTIALS: the same pair the sentry-logs skill uses -
    k15/secrets.json   ->  "sentryReadToken"        (org:read scope)
    k15/config.json    ->  "sentry": {orgId, projectId}
    or env             ->  SENTRY_READ_TOKEN, SENTRY_ORG, SENTRY_PROJECT

A session id is OUR id: the `session` in the JSONL, the `session.id` and
`gen_ai.conversation.id` on the span. That is the join - Sentry says what the
model did, the logs say what the system did around it.

FIELD NAMES: spans arrive over OTLP from Pipecat, so the gen_ai.* attributes
are whatever Pipecat emitted plus the ones tracing.py adds. --json shows what
the API actually returned; prefer it over guessing when a column is empty.
"""
import argparse
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://sentry.io/api/0"
REPO = pathlib.Path(__file__).resolve().parents[3]

BASE_FIELDS = ("timestamp", "trace", "span.op", "span.description",
               "span.duration")
GEN_AI = ("gen_ai.request.model", "gen_ai.usage.input_tokens",
          "gen_ai.usage.output_tokens", "gen_ai.tool.name")
IO_FIELDS = ("gen_ai.input.messages", "gen_ai.output.messages",
             "gen_ai.tool.call.arguments", "gen_ai.tool.call.result")


def repo_roots():
    roots = [REPO]
    if REPO.parent.name == "worktrees" and REPO.parent.parent.name == ".claude":
        roots.append(REPO.parent.parent.parent)
    return roots


def _read(root, name):
    try:
        return json.loads((root / "k15" / name).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def credentials():
    import os
    token = os.environ.get("SENTRY_READ_TOKEN")
    org = os.environ.get("SENTRY_ORG")
    project = os.environ.get("SENTRY_PROJECT")
    for root in repo_roots():
        s, c = _read(root, "secrets.json"), _read(root, "config.json")
        if not (s or c):
            continue
        token = token or s.get("sentryReadToken")
        sentry = c.get("sentry") or {}
        org = org or sentry.get("orgId")
        project = project or sentry.get("projectId")
        break
    if not token or "..." in str(token) or not org or not project:
        sys.exit(
            "No Sentry read credentials (worktrees fall back to the enclosing\n"
            "checkout's k15/ files - NEITHER had them).\n"
            '  k15/secrets.json:  "sentryReadToken": "sntrys_..."\n'
            '  k15/config.json :  "sentry": {"orgId": "...", "projectId": "..."}\n'
            "  The token needs org:read; the DSN key in config.json cannot query.")
    return token, str(org), str(project)


def since_ok(spec):
    if not re.fullmatch(r"\d+[smhdw]", spec.strip()):
        sys.exit(f"bad --since {spec!r}; use 30m, 6h, 7d")
    return spec.strip()


def fetch(query, fields, since, limit=100, sort="-timestamp"):
    token, org, project = credentials()
    params = [("dataset", "spans"), ("project", project), ("query", query),
              ("statsPeriod", since), ("sort", sort),
              ("per_page", str(min(100, limit)))]
    params += [("field", f) for f in fields]
    url = f"{API}/organizations/{org}/events/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("data", [])
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        hint = {401: "  -> token missing or lacking org:read",
                400: "  -> an unknown field; try --json"}.get(e.code, "")
        sys.exit(f"HTTP {e.code} from Sentry\n{detail}\n{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach sentry.io: {e.reason}")


def ms(v):
    try:
        return f"{float(v):.0f}ms"
    except (TypeError, ValueError):
        return "-"


def show_rows(rows, io_on):
    for r in rows:
        when = str(r.get("timestamp", ""))[5:19].replace("T", " ")
        op = r.get("span.op") or "-"
        desc = r.get("span.description") or ""
        line = f"{when}  {ms(r.get('span.duration')):>8}  {op:22} {desc}"
        tin = r.get("gen_ai.usage.input_tokens")
        tout = r.get("gen_ai.usage.output_tokens")
        if tin or tout:
            line += f"   tokens={tin or 0}/{tout or 0}"
        model = r.get("gen_ai.request.model")
        if model:
            line += f" {model}"
        print(line)
        if io_on:
            for f in IO_FIELDS:
                v = r.get(f)
                if v:
                    print(f"      {f.split('.')[-1]}: {str(v)[:2000]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=("conversations", "session", "trace", "tools"))
    ap.add_argument("ident", nargs="?", help="session id or trace id")
    ap.add_argument("--since", default="24h", help="30m, 6h, 7d (default 24h)")
    ap.add_argument("--io", action="store_true",
                    help="include prompts, completions and tool arguments")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--json", action="store_true", help="raw API rows")
    a = ap.parse_args()
    since = since_ok(a.since)

    if a.command == "conversations":
        # One row per voice session: group by the conversation id tracing.py
        # sets from our session id.
        rows = fetch("has:gen_ai.conversation.id",
                     ("gen_ai.conversation.id", "count()",
                      "sum(span.duration)"), since, a.limit, "-count()")
        if a.json:
            print(json.dumps(rows, indent=1))
            return
        if not rows:
            print(f"(no conversations in the last {since})")
            return
        for r in rows:
            print(f"{r.get('gen_ai.conversation.id', '?'):12} "
                  f"spans={r.get('count()', 0):<5} "
                  f"total={ms(r.get('sum(span.duration)'))}")
        print(f"\n{len(rows)} conversation(s) in the last {since}."
              "\nJoin to the logs:  python .claude/skills/sentry-logs/query.py"
              " --session <id>")
        return

    if a.command == "tools":
        rows = fetch("span.op:gen_ai.execute_tool",
                     ("gen_ai.tool.name", "count()", "avg(span.duration)"),
                     since, a.limit, "-count()")
        if a.json:
            print(json.dumps(rows, indent=1))
            return
        for r in rows:
            print(f"{str(r.get('gen_ai.tool.name', '?')):24} "
                  f"calls={r.get('count()', 0):<5} "
                  f"avg={ms(r.get('avg(span.duration)'))}")
        if not rows:
            print(f"(no tool spans in the last {since})")
        return

    if not a.ident:
        sys.exit(f"{a.command} needs an id")
    query = (f"gen_ai.conversation.id:{a.ident}" if a.command == "session"
             else f"trace:{a.ident}")
    fields = BASE_FIELDS + GEN_AI + (IO_FIELDS if a.io else ())
    # Oldest first: a trace reads as a timeline.
    rows = fetch(query, fields, since, a.limit, "timestamp")
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    if not rows:
        print(f"(nothing for {a.ident} in the last {since};"
              " spans are full-fidelity for 30 days)")
        return
    show_rows(rows, a.io)
    print(f"\n{len(rows)} span(s)")


if __name__ == "__main__":
    main()
