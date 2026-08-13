"""Tier-3 worker adapters: each vendor's agent harness as a headless,
subscription-billed CLI subprocess. Contract: run(task, timeout) -> dict(ok,
summary, detail). The adapters own every vendor-specific flag and output
shape, so nothing above this file knows or cares which harness ran - swapping
is config.workerProvider, exactly like assistantProvider (and keyed by the
same vendor names: anthropic runs the claude CLI, openai runs codex).

Fail-soft, like every other lane: a missing CLI disables background tasks with
a clear startup + doctor message, never a crash. run() never raises - every
failure mode is a truthful FAILED result the voice lane can speak.

Both CLIs read worker_home/AGENTS.md as the standing briefing; the prompt is
fed via STDIN (both CLIs accept it), which sidesteps cmd.exe quoting for the
.cmd shims npm installs."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER_HOME = HERE / "worker_home"

PROMPT = (
    "Background job from the couch voice assistant. Read AGENTS.md in this "
    "directory first - it is the standing briefing and the output contract.\n"
    "\nTASK: {task}\n\n"
    'Remember: reply with ONLY the JSON object {{"summary": ..., "detail": '
    "...}} the contract describes.")


def _argv_for(path):
    """subprocess can't spawn .cmd/.bat shims directly (CreateProcess wants a
    real executable); route those through cmd.exe. Native .exe installs pass
    through untouched."""
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", path]
    return [path]


def parse_reply(text):
    """The contract JSON out of a CLI's final text - tolerant of code fences
    and surrounding prose (outermost {...} wins). Fallback: whole text as
    detail, first sentence as summary, so a worker that ignored the contract
    still yields something speakable."""
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
        # Both come from config, per vendor and spelled out there - no hidden
        # adapter defaults to hunt for. Empty means "whatever this CLI is set
        # to", which is a choice config states, not one this file makes.
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
                self._argv(), input=PROMPT.format(task=task),
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
            # Auth is the one failure that needs a HUMAN, not a retry: tokens
            # live in the profile of whoever ran `<cli> login` and survive
            # reboots, but a password/subscription change or a revocation
            # invalidates them silently - and "the task failed" from across
            # the room says nothing about what to do next.
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
    """A tool's input rendered for a span attribute - the search query or the
    URL, not the whole argument blob."""
    if isinstance(v, dict):
        for k in ("query", "url", "pattern", "command", "file_path", "prompt"):
            if v.get(k):
                return str(v[k])[:n]
        return json.dumps(v, default=str)[:n]
    return str(v or "")[:n]


def result_meta(d):
    """The fields `claude -p` returns alongside `result` and we used to drop.

    Names verified against the CLI on 2026-08-12 rather than assumed - they
    are not in the published reference. Anything absent is omitted rather
    than guessed, so a CLI that renames one loses a span attribute and
    nothing else."""
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
    """claude -p. Research-only BY CONSTRUCTION: no Bash in --allowedTools,
    so the boundary is structural, not a prompt rule. A worker ingests
    untrusted web content while running as the account that holds the gaming
    PC's ssh key and the VirtualHere PIN - and a shell reads files the Read
    deny rules cannot see, so "AGENTS.md says don't" was the only thing
    between an injected page and exfiltration. Now the harness is: file tools
    confined to worker_home, deny rules as the secrets backstop, and no shell
    at all. Actions stay where they always belonged - Tier 2's dispatch,
    after the user asks. (CodexWorker below CANNOT make this promise: its
    sandbox confines writes, not reads or shell - doctor warns when that lane
    is selected.) The injection canary drill (voice-testing 10c) proves all
    of it on this machine.

    Output is stream-json so the TOOL CALLS are visible: with plain --output-
    format json the only artefact of three minutes of research is the final
    text, which is why "what did it actually look at" was unanswerable. The
    stream is newline-delimited SDK messages (system / assistant / user /
    result); we read tool_use blocks off the assistant messages and take the
    final `type: result` object exactly as before.

    Format churn is the known risk here - CodexWorker below avoids codex's
    JSONL entirely for that reason. So the parse is defensive on both ends:
    unrecognised lines are skipped, a stream with no result object falls back
    to the old whole-stdout parse, and a CLI that rejects the flags at all
    retries once in legacy mode (see run). Churn costs tool spans, never the
    job."""
    exe = "claude"
    TOOLS = "WebSearch,WebFetch,Read,Glob,Grep,Write"

    def __init__(self, model="", effort=""):
        super().__init__(model, effort)
        self.stream = True          # flipped off permanently by a usage error

    def _argv(self):
        argv = _argv_for(self.path) + ["-p"]
        argv += (["--output-format", "stream-json", "--verbose"] if self.stream
                 else ["--output-format", "json"])
        argv += ["--allowedTools", self.TOOLS]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def run(self, task, timeout):
        r = super().run(task, timeout)
        if r["ok"] or not self.stream:
            return r
        # A CLI that does not know these flags fails in milliseconds with a
        # usage error. Distinguish that from a real task failure and retry
        # once in the old format, so an older or newer claude degrades to
        # "no tool spans" instead of "background tasks are broken".
        blurb = f"{r.get('detail', '')}".lower()
        if any(w in blurb for w in ("unknown option", "unrecognized",
                                    "invalid option", "usage:",
                                    "requires --verbose", "--verbose")):
            self.stream = False
            r = super().run(task, timeout)
            r.setdefault("meta", {})["stream_fallback"] = True
        return r

    def _env(self):
        # Claude Code's depth knob is an env var, not a flag. Its own default
        # is already high on the adaptive-reasoning models, so an empty
        # workerEffort inherits that rather than fighting it.
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
            # Not a stream: legacy --output-format json, or a shape we don't
            # know. Old behaviour verbatim.
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
        out["steps"] = steps
        return out


class CodexWorker(_CliWorker):
    """codex exec: prompt on stdin via the '-' sentinel; --output-last-message
    writes the final text to a file of our choosing, which sidesteps its
    JSONL event-shape churn entirely. Guardrail: the workspace-write sandbox
    rooted here (network on - research needs it)."""
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
            # Codex defaults to medium - tuned for interactive work, which is
            # the wrong trade in a lane where latency costs nothing. -c values
            # are TOML, so the string needs its quotes.
            argv += ["-c", f'model_reasoning_effort="{self.effort}"']
        return argv + ["-"]

    def _extract(self, p):
        try:
            text = self.LAST.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = p.stdout
        return parse_reply(text)


# Keyed by VENDOR, like assistantProvider - so one vocabulary spans both
# lanes and workerProvider lines up with workerModel<Vendor>.
WORKERS = {"anthropic": ClaudeWorker, "openai": CodexWorker}
MODEL_KEY = {"anthropic": "workerModelAnthropic", "openai": "workerModelOpenai"}
