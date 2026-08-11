"""Tier-3 worker adapters: each vendor's agent harness as a headless,
subscription-billed CLI subprocess. Contract: run(task, timeout) -> dict(ok,
summary, detail). The adapters own every vendor-specific flag and output
shape, so nothing above this file knows or cares which harness ran - swapping
is config.workerProvider, exactly like assistantProvider.

Fail-soft (rule 1's convention, new lane): a missing CLI disables the lane
with a clear startup + doctor message, never a crash. run() never raises -
every failure mode is a truthful FAILED result the voice lane can speak.

Both CLIs read worker_home/AGENTS.md as the standing briefing; the prompt is
fed via STDIN (both CLIs accept it), which sidesteps cmd.exe quoting for the
.cmd shims npm installs. Exact flag sets are pinned live at the K15 deploy
drill (ledger rows D9/D10)."""
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
        self.model = model
        self.effort = effort                    # "" = the CLI's own default
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


class ClaudeWorker(_CliWorker):
    """claude -p: prompt on stdin, one JSON object on stdout, final text in
    its 'result' field. Guardrails: the --allowedTools list here plus
    worker_home/.claude/settings.json deny rules (secrets, out-of-scope
    writes) - the injection canary drill proves both."""
    exe = "claude"

    def _argv(self):
        argv = _argv_for(self.path) + [
            "-p", "--output-format", "json",
            "--allowedTools", "WebSearch,WebFetch,Read,Glob,Grep,Write,Bash",
        ]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def _env(self):
        # Claude Code's depth knob is an env var, not a flag. Its own default
        # is already high on the adaptive-reasoning models, so an empty
        # workerEffort inherits that rather than fighting it.
        if not self.effort:
            return None
        return {**os.environ, "CLAUDE_CODE_EFFORT_LEVEL": self.effort}

    def _extract(self, p):
        try:
            return parse_reply(json.loads(p.stdout)["result"])
        except (ValueError, KeyError, TypeError):
            return parse_reply(p.stdout)


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


WORKERS = {"claude": ClaudeWorker, "codex": CodexWorker}
