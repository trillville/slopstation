"""Query the couch system's logs in Sentry, from the terminal.

    python query.py --level error --since 24h
    python query.py --turn 9f2c1a --since 24h          # both machines, one intent
    python query.py --lane voice --event gate_miss
    python query.py --query 'event:host_ready dur_ms:>20000' --since 7d

Prefer the flags: PowerShell strips double quotes when passing a raw query to
a native process, and the API then filters on something nobody typed.

Stdlib only, so it runs from any checkout without a venv.

CREDENTIALS (read-only; the ingest key in config.json cannot query):
    k15/secrets.json   ->  "sentryReadToken"        (org:read scope)
    k15/config.json    ->  "sentry": {orgId, projectId}
    or env             ->  SENTRY_READ_TOKEN, SENTRY_ORG, SENTRY_PROJECT
On a worktree (<repo>/.claude/worktrees/<name>) the gitignored files do not
exist locally; the enclosing checkout's copies are read automatically.

FIELD NAMES: our attributes reach Sentry through the OTLP log pipeline, so
they are addressable by the name events.py wrote (lane, event, turn, ...).
The rendered line is parsed out of the log body, which is the original JSONL
line - so a field this script does not know still prints. Use --json to see
exactly what the API returned.
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

# Rendered first, in this order - the rest of a line's fields follow as k=v.
LEAD = ("event", "turn", "session", "dur_ms", "err")

# Asked for on every query. The body carries everything else.
FIELDS = ("timestamp", "severity", "message")


def repo_roots():
    """This checkout first; then, when it is a worktree under
    <repo>/.claude/worktrees/<name>, the enclosing checkout - gitignored
    files (secrets.json, config.json) exist only in the main checkout."""
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
            "  The token needs org:read (Settings -> Auth Tokens). The DSN\n"
            "  public key in config.json is an INGEST key and cannot query.")
    return token, str(org), str(project)


def since_ok(spec):
    if not re.fullmatch(r"\d+[smhdw]", spec.strip()):
        sys.exit(f"bad --since {spec!r}; use 30m, 6h, 7d")
    return spec.strip()


def fetch(query, since, limit, extra_fields=()):
    """One page per 100 rows, following the cursor until limit is reached."""
    token, org, project = credentials()
    fields = list(FIELDS) + [f for f in extra_fields if f not in FIELDS]
    rows, cursor = [], None
    while len(rows) < limit:
        params = [("dataset", "logs"), ("project", project),
                  ("query", query), ("statsPeriod", since),
                  ("sort", "-timestamp"),
                  ("per_page", str(min(100, limit - len(rows))))]
        params += [("field", f) for f in fields]
        if cursor:
            params.append(("cursor", cursor))
        url = f"{API}/organizations/{org}/events/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read())
                link = r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            hint = {
                401: "  -> the token is missing or lacks org:read",
                403: "  -> the token cannot see this organization",
                400: "  -> an unknown field or a malformed query; try --json",
            }.get(e.code, "")
            sys.exit(f"HTTP {e.code} from Sentry\n{detail}\n{hint}")
        except urllib.error.URLError as e:
            sys.exit(f"cannot reach sentry.io: {e.reason}")
        page = body.get("data", [])
        rows += page
        # Sentry paginates by Link header; results="true" means more to come.
        m = re.search(r'<([^>]+)>;\s*rel="next";\s*results="true";\s*cursor="([^"]+)"',
                      link)
        if not page or not m:
            break
        cursor = m.group(2)
    return rows


def render(row):
    when = str(row.get("timestamp", ""))[5:19].replace("T", " ")
    sev = str(row.get("severity", "info")).lower()
    mark = {"error": "ERROR ", "warn": "WARN  ", "fatal": "FATAL "}.get(sev, "")
    body = row.get("message") or ""
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        where = f"{row.get('service', '?')}/{row.get('lane', '?')}"
        return f"{when}  {where:20} {mark}{str(body).rstrip()}"
    where = f"{d.get('service', '?')}/{d.get('lane', '?')}"
    skip = {"ts", "level", "env", "service", "lane", "host"}
    parts = [str(d[k]) if k == "event" else f"{k}={d[k]}"
             for k in LEAD if k in d]
    parts += [f"{k}={v}" for k, v in d.items()
              if k not in skip and k not in LEAD]
    return f"{when}  {where:20} {mark}{' '.join(parts)}"


def build(a):
    """Compose a Sentry search from flags, so the common questions need no
    quoting. Attribute names are ours - the four that were Loki labels have
    no special status here, which is why turn and session need no `| json |`
    equivalent."""
    terms = [f"env:{a.env}"]
    for flag, attr in (("service", "service"), ("lane", "lane"),
                       ("event", "event"), ("turn", "turn"),
                       ("session", "session")):
        v = getattr(a, flag)
        if v:
            terms.append(f"{attr}:{v}" if "|" not in v
                         else f"{attr}:[{','.join(v.split('|'))}]")
    if a.level:
        terms.append(f"severity:{a.level}" if "|" not in a.level
                     else f"severity:[{','.join(a.level.split('|'))}]")
    if a.contains:
        terms.append(f'"{a.contains}"')
    return " ".join(terms)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", help="raw Sentry search (or use the flags below)")
    ap.add_argument("--service", help="k15 | gamepc")
    ap.add_argument("--lane", help="voice | launch | listener | supervisor | ...")
    ap.add_argument("--level", help="error | warn | info")
    ap.add_argument("--event", help="host_ready, launch_failed, gate_miss, ...")
    ap.add_argument("--turn", help="follow one intent across BOTH machines")
    ap.add_argument("--session", help="one voice conversation")
    ap.add_argument("--contains", help="plain substring, matched on the body")
    ap.add_argument("--env", default="prod", help="prod (default) | test")
    ap.add_argument("--since", default="6h", help="30m, 6h, 7d (default 6h)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--field", action="append", default=[],
                    help="request an extra attribute as a column (repeatable)")
    ap.add_argument("--json", action="store_true", help="raw API rows")
    a = ap.parse_args()

    query = a.query or build(a)
    if not a.query:
        print(f"# {query}\n")

    rows = fetch(query, since_ok(a.since), a.limit, a.field)
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    if not rows:
        print(f"(no results in the last {a.since})")
        return
    # The API sorts newest first; read oldest first, like the log itself.
    for row in reversed(rows):
        print(render(row))
    print(f"\n{len(rows)} line(s) in the last {a.since}")


if __name__ == "__main__":
    main()
