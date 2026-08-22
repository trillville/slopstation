"""Tier-3 worker adapters: each vendor's agent CLI as a headless subprocess.
Contract: run(task, timeout) -> dict(ok, summary, detail); run() never raises.
Chosen by config.workerProvider (anthropic = claude CLI, openai = codex).

Both CLIs read worker_home/AGENTS.md as the standing briefing; the prompt goes
in on STDIN, which sidesteps cmd.exe quoting for npm's .cmd shims."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER_HOME = HERE / "worker_home"

PROMPT = (
    "Background job from the couch voice assistant. AGENTS.md in this "
    "directory is your standing briefing and output contract - your harness "
    "has already loaded it, and you have no file tools to re-read it with.\n"
    "{library}"
    "\nTASK: {task}\n\n"
    'Remember: reply with ONLY the JSON object {{"summary": ..., "detail": '
    "...}} the contract describes.")


def _library_context():
    """Catalog and today's Steam prices, inline in the prompt - the worker has
    no file tools (ClaudeWorker.DENY) to read them with. "" on any error."""
    try:
        import library
        rows = library.catalog_lines()
        deals = library.load_deals() or {}
    except Exception:
        return ""
    if not rows:
        return ""
    out = ["\nThe user's STEAM CATALOG follows. Treat it as ground truth about "
           "what they own and how long they have played - it is fresher than "
           "anything you will find on the web.",
           "appid|name|tags|genres|hours|lastPlayed|installed|controller",
           "\n".join(rows)]
    ws, sp = deals.get("wishlist_on_sale") or [], deals.get("specials") or []
    if ws or sp:
        out.append("\nSteam prices already fetched for you - use these rather "
                   "than searching for prices:")
        if ws:
            out.append("wishlist items now discounted: " + json.dumps(ws[:12]))
        if sp:
            out.append("featured specials: " + json.dumps(sp[:12]))
    return "\n".join(out) + "\n"


def _argv_for(path):
    """CreateProcess can't spawn .cmd/.bat shims directly; route those through
    cmd.exe."""
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", path]
    return [path]


def parse_reply(text):
    """The contract JSON out of a CLI's final text; tolerant of code fences and
    prose (outermost {...} wins). If the worker ignored the contract: whole
    text as detail, first sentence as summary."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and d.get("summary"):
                summary = str(d["summary"]).strip()
                return {"summary": summary,
                        "detail": str(d.get("detail", "")).strip() or summary}
        except ValueError:
            pass
    text = (text or "").strip()
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return {"summary": first[:300] or "the task finished with no output",
            "detail": text or "no output"}


class _CliWorker:
    exe = ""                                    # shim/binary name on PATH

    def __init__(self, model="", effort=""):
        # Both from config, per vendor. Empty = the CLI's own default.
        self.model = model
        self.effort = effort
        self.path = shutil.which(self.exe)

    def available(self):
        return self.path is not None

    def _env(self):
        """Adapters whose depth knob lives in the environment override this."""
        return None

    def run(self, task, timeout):
        try:
            p = subprocess.run(
                self._argv(),
                input=PROMPT.format(task=task, library=_library_context()),
                cwd=str(WORKER_HOME), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                env=self._env())
        except subprocess.TimeoutExpired:
            return {"ok": False, "summary": "the task ran out of time",
                    "detail": f"{self.exe} exceeded {timeout}s and was killed"}
        except OSError as e:
            return {"ok": False, "summary": "the worker failed to start",
                    "detail": f"{self.exe}: {e}"}
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip()[-500:]
            # Auth is the one failure needing a human, not a retry: a password
            # change or revocation kills a CLI token silently.
            need_login = any(w in tail.lower() for w in (
                "not logged in", "log in", "login", "unauthorized",
                "authenticate", "authentication", "401", "expired"))
            return {"ok": False,
                    "summary": (f"the background worker needs a re-login - run "
                                f"{self.exe} login on the console"
                                if need_login else "the task failed"),
                    "detail": f"{self.exe} exit {p.returncode}: {tail}"}
        return {"ok": True, **self._extract(p)}


def _short(v, n=300):
    """A tool's input rendered for a span attribute - the query or URL, not the
    whole argument blob."""
    if isinstance(v, dict):
        for k in ("query", "url", "pattern", "command", "file_path", "prompt"):
            if v.get(k):
                return str(v[k])[:n]
        return json.dumps(v, default=str)[:n]
    return str(v or "")[:n]


def result_meta(d):
    """Cost/usage fields `claude -p` returns alongside `result`. Undocumented
    names, so anything absent is omitted - a rename costs one span
    attribute."""
    usage = d.get("usage") or {}
    server = usage.get("server_tool_use") or {}
    models = d.get("modelUsage") or {}
    first = next(iter(models.values()), {}) if models else {}
    meta = {
        "cost_usd": d.get("total_cost_usd"),
        "turns": d.get("num_turns"),
        "api_ms": d.get("duration_api_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "web_searches": server.get("web_search_requests"),
        "web_fetches": server.get("web_fetch_requests"),
        "denials": len(d.get("permission_denials") or []) or None,
        "stop_reason": d.get("stop_reason"),
        "model": first.get("canonicalModel") or (next(iter(models), None)),
        "cli_session": d.get("session_id"),
    }
    return {k: v for k, v in meta.items() if v is not None}


class ClaudeWorker(_CliWorker):
    """claude -p, restricted to research.

    The boundary is --disallowedTools; in -p mode --allowedTools is only an
    AUTO-APPROVE list and restricts nothing (2026-08-14). This lane reads
    untrusted web content on the box holding secrets.json and the gamepc key.
    DENY comes from a live tool enumeration and is guarded by
    bench/probe_worker_surface.py - the CLI's tool set grows without notice.

    Output is stream-json so tool calls are visible: newline-delimited SDK
    messages (system / assistant / user / result), tool_use blocks off the
    assistant messages, the answer off the final result object. Format churn
    costs only tool spans - see the fallbacks in _extract and run."""
    exe = "claude"
    # No file tools: Read/Write CANNOT be path-scoped (2026-08-14: a Read(**/x)
    # deny was ignored and the file read anyway), so the library is passed in
    # instead - see _library_context.
    TOOLS = "WebSearch,WebFetch"
    # Everything else the CLI offered on 2026-08-14, grouped by what it buys
    # an injected instruction.
    DENY = ("Bash,PowerShell,"                                   # execution
            "Read,Glob,Grep,Write,"                              # the filesystem, at all
            "Edit,NotebookEdit,"                                 # writes outside worker_home
            "CronCreate,CronDelete,CronList,ScheduleWakeup,"     # persistence
            "Artifact,PushNotification,SendMessage,RemoteTrigger,"   # exfiltration
            "Agent,Workflow,TaskCreate,TaskGet,TaskList,"        # more agents
            "TaskOutput,TaskStop,TaskUpdate,"
            "Skill,ToolSearch,Monitor,DesignSync,"               # surface expansion
            "EnterWorktree,ExitWorktree,ReportFindings")

    def __init__(self, model="", effort=""):
        super().__init__(model, effort)
        self.stream = True          # flipped off permanently by a usage error

    def _argv(self):
        argv = _argv_for(self.path) + ["-p"]
        argv += (["--output-format", "stream-json", "--verbose"] if self.stream
                 else ["--output-format", "json"])
        # Both flags: allowedTools auto-approves, disallowedTools is what
        # actually removes a tool from the model's list.
        argv += ["--allowedTools", self.TOOLS, "--disallowedTools", self.DENY]
        # No MCP: the subprocess otherwise inherits the desktop account's
        # connectors - the surface canary caught eleven Drive tools on its
        # first run, enough to read, overwrite, trash and publicly share the
        # user's Drive. Naming those eleven in DENY would miss the next
        # connector, so the config is emptied and --strict-mcp-config stops
        # any config adding one back.
        argv += ["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def run(self, task, timeout):
        r = super().run(task, timeout)
        if r["ok"] or not self.stream:
            return r
        # A CLI that does not know these flags fails in milliseconds with a
        # usage error; retry in the old format rather than lose the job.
        blurb = f"{r.get('detail', '')}".lower()
        if any(w in blurb for w in ("unknown option", "unrecognized",
                                    "invalid option", "usage:",
                                    "requires --verbose", "--verbose")):
            self.stream = False
            r = super().run(task, timeout)
            r.setdefault("meta", {})["stream_fallback"] = True
        return r

    def _env(self):
        # Claude Code's depth knob is an env var, not a flag.
        if not self.effort:
            return None
        return {**os.environ, "CLAUDE_CODE_EFFORT_LEVEL": self.effort}

    def _extract(self, p):
        steps, results, final = [], {}, None
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue                        # banners, blanks, whatever else
            try:
                ev = json.loads(line)
            except ValueError:
                continue                        # a partial line is not a failure
            kind = ev.get("type")
            if kind == "result":
                final = ev                      # last one wins
                continue
            for block in ((ev.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    steps.append({"tool": block.get("name") or "tool",
                                  "input": _short(block.get("input")),
                                  "id": block.get("id")})
                elif block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = " ".join(str(x.get("text", "")) for x in c
                                     if isinstance(x, dict))
                    results[block.get("tool_use_id")] = str(c or "")[:2000]
        if final is None:
            # Legacy --output-format json, or an unknown shape.
            try:
                final = json.loads(p.stdout)
            except ValueError:
                return parse_reply(p.stdout)
        if not isinstance(final, dict):
            return parse_reply(p.stdout)
        for s in steps:
            s["result"] = results.get(s.pop("id"), "")
        out = parse_reply(final.get("result", ""))
        out["meta"] = result_meta(final)
        # Count searches from the STEPS: usage.server_tool_use only counts the
        # API's server-side web_search, so it reads 0 for this CLI's
        # harness-side WebSearch/WebFetch (2026-08-14).
        for field, tool in (("web_searches", "WebSearch"),
                            ("web_fetches", "WebFetch")):
            n = sum(1 for s in steps if s.get("tool") == tool)
            if n:
                out["meta"][field] = n
        out["steps"] = steps
        return out


class CodexWorker(_CliWorker):
    """codex exec: prompt on stdin via the '-' sentinel; --output-last-message
    writes the final text to a file, sidestepping its JSONL event-shape churn.
    The workspace-write sandbox (network on) confines writes but not reads or
    shell, so doctor warns on this lane."""
    exe = "codex"
    LAST = WORKER_HOME / ".last-message.txt"

    def _argv(self):
        argv = _argv_for(self.path) + [
            "exec", "--skip-git-repo-check",
            "--sandbox", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
            "--output-last-message", str(self.LAST),
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            # Codex defaults to medium. -c values are TOML, so the string
            # needs its quotes.
            argv += ["-c", f'model_reasoning_effort="{self.effort}"']
        return argv + ["-"]

    def _extract(self, p):
        try:
            text = self.LAST.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = p.stdout
        return parse_reply(text)


# Keyed by vendor, like assistantProvider; workerProvider lines up with
# workerModel<Vendor>.
WORKERS = {"anthropic": ClaudeWorker, "openai": CodexWorker}
WORKER_MODEL_KEY = {"anthropic": "workerModelAnthropic", "openai": "workerModelOpenai"}
