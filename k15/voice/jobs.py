"""Tier-3 job store + runner. state/jobs.json is the one home for job state,
reconciled at startup (house rule: every piece of distributed state has a
reconciler). One worker thread, small queue, truthful failures - a job can
end DONE or FAILED, never vanish.

Read/unread is the announcement contract: a finished job is unread until its
summary has actually been HEARD (full announcement playback, or a "what did
you find" retrieval) - an aborted announcement leaves it unread, so the
next-wake mention picks it up."""
import json
import threading
import time
import uuid

from pathlib import Path

import events
import tracing

HERE = Path(__file__).resolve().parent
JOBS_FILE = HERE.parent / "state" / "jobs.json"

QUEUED, RUNNING, DONE, FAILED = "QUEUED", "RUNNING", "DONE", "FAILED"
KEEP = 10                       # finished jobs retained in the file
QUEUE_CAP = 3                   # queued-or-running ceiling; beyond = busy

# What a session's assistant is told about recent background work. Bounded on
# all three axes because it rides in every LLM turn of that session: only
# results fresh enough that "which one was cheapest?" plausibly means THEM,
# only the last couple, and only the readable head of a long report.
CONTEXT_AGE_S = 6 * 3600
CONTEXT_JOBS = 2
CONTEXT_DETAIL_CHARS = 1200


class JobStore:
    """adapter = a workers.*Worker (never None - the caller gates the lane);
    on_done = fn(job dict), called off-thread when a job finishes."""

    # A worker holds a shell and reaches the CLIs directly, so --dry-run
    # can't gate its side effects the way Dispatch gates Tier 1/2. The honest
    # thing is to tell it: advisory, not enforcement (AGENTS.md already says
    # side effects need an explicit ask), so a dry-run drill doesn't start
    # sessions on a TV someone is watching.
    DRY_NOTE = ("[The couch system is in DRY-RUN: research and report only. "
                "Do not run couch.py, exlink.py, or any other command that "
                "changes system state.] ")

    def __init__(self, log, adapter, timeout_s, on_done=None, dry_run=False):
        self.log = log
        self.adapter = adapter
        self.timeout_s = timeout_s
        self.on_done = on_done
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._kick = threading.Event()
        # The user's last utterance, written by GrammarGate. A job stores it
        # so the replay can quote the person instead of the model - see
        # enqueue and voice_agent.job_messages. None on the chord lane and
        # the REPL, which have no transcript to quote.
        self.asked = None

    # -- the state file (all access under the lock) ---------------------------

    def _load(self):
        try:
            return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, jobs):
        JOBS_FILE.parent.mkdir(exist_ok=True)
        live = [j for j in jobs if j["status"] in (QUEUED, RUNNING)]
        done = [j for j in jobs if j["status"] not in (QUEUED, RUNNING)]
        JOBS_FILE.write_text(json.dumps(live + done[-KEEP:], indent=1),
                             encoding="utf-8")

    def _update(self, job_id, **fields):
        with self._lock:
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id:
                    j.update(fields)
            self._save(jobs)

    # -- lifecycle ------------------------------------------------------------

    def reconcile(self):
        """RUNNING jobs from a dead process -> FAILED, announced via unread.
        Returns how many were orphaned."""
        with self._lock:
            jobs = self._load()
            orphans = [j for j in jobs if j["status"] == RUNNING]
            for j in orphans:
                j.update(status=FAILED, read=False, finished=int(time.time()),
                         summary="a background task was lost to a restart",
                         detail="the voice agent restarted while this job ran; "
                                "ask again to re-run it")
            self._save(jobs)
        for j in orphans:
            self.log.warn("job_orphaned", job=j["id"], reason="restart")
        return len(orphans)

    def start(self):
        threading.Thread(target=self._run_loop, daemon=True,
                         name="job-worker").start()

    def enqueue(self, task):
        """-> (ok, spoken detail). Truthful busy beyond the cap."""
        with self._lock:
            jobs = self._load()
            active = [j for j in jobs if j["status"] in (QUEUED, RUNNING)]
            if len(active) >= QUEUE_CAP:
                return False, f"{len(active)} tasks are already in flight"
            job = {"id": uuid.uuid4().hex[:8], "task": task, "status": QUEUED,
                   "provider": self.adapter.exe, "created": int(time.time()),
                   "summary": "", "detail": "", "read": True,
                   # The conversation span that asked for this, frozen as a
                   # W3C traceparent. Captured HERE because this is the last
                   # moment it is still the active span - the worker runs on
                   # another thread minutes later. None when tracing is off,
                   # and it rides in the state file so a job that outlives a
                   # restart still reports under the turn that queued it.
                   "trace": tracing.carrier(),
                   "session": events.current().get("session"),
                   # What the USER said, next to the brief the MODEL wrote.
                   # Keeping both is the point: the replay needs a true user
                   # turn, and the brief is the model's own words - presenting
                   # it as the user's put a bad instruction ("using only games
                   # in the provided catalog") into six hours of context as
                   # though it had been asked for.
                   "asked": self.asked}
            self._save(jobs + [job])
        self.log("job_queued", job=job["id"], task=task[:200])
        self._kick.set()
        return True, "queued - the result will be announced"

    def _task_text(self, job):
        return (self.DRY_NOTE + job["task"]) if self.dry_run else job["task"]

    def _next_queued(self):
        with self._lock:
            jobs = self._load()
            for j in jobs:
                if j["status"] == QUEUED:
                    j["status"] = RUNNING
                    self._save(jobs)
                    return dict(j)
        return None

    def _run_loop(self):
        while True:
            self._kick.wait()
            self._kick.clear()
            while True:
                job = self._next_queued()
                if job is None:
                    break
                self.log("job_running", job=job["id"], provider=self.adapter.exe,
                         dry_run=self.dry_run or None)
                t0 = time.time()
                with tracing.job_span(job["id"], job["task"],
                                      job.get("trace"), job.get("session"),
                                      self.adapter.exe) as jspan:
                    r = self.adapter.run(self._task_text(job), self.timeout_s)
                    meta = r.get("meta") or {}
                    for s in r.get("steps") or []:
                        jspan.step(s.get("tool"), s.get("input"),
                                   s.get("result"))
                    jspan.finish(r["summary"], r["detail"], meta)
                status = DONE if r["ok"] else FAILED
                self._update(job["id"], status=status, read=False,
                             finished=int(time.time()),
                             summary=r["summary"], detail=r["detail"])
                emit = self.log if r["ok"] else self.log.error
                # What it cost and what it touched, not just that it ended.
                # job_done used to carry status=DONE alone, so "what came
                # back" was unanswerable from the logs at all.
                emit("job_done" if r["ok"] else "job_failed", job=job["id"],
                     status=status, dur_ms=round((time.time() - t0) * 1000),
                     session=job.get("session"), summary=r["summary"][:200],
                     tools=len(r.get("steps") or []) or None,
                     **{k: meta[k] for k in
                        ("cost_usd", "turns", "web_searches", "web_fetches",
                         "denials", "model", "stop_reason")
                        if k in meta})
                if self.on_done:
                    job.update(status=status, summary=r["summary"],
                               detail=r["detail"])
                    try:
                        self.on_done(job)
                    except Exception as e:
                        self.log.error("job_announce_hook_failed",
                                       job=job["id"], err=repr(e))

    # -- the voice surface ----------------------------------------------------

    def cancel_queued(self):
        """Cancel every QUEUED job. A RUNNING subprocess is not killed - the
        honest answer is that it finishes or times out, since killing an
        agent's child process tree cleanly on Windows is real work for a rare
        want. Returns (n_cancelled, running_now)."""
        with self._lock:
            jobs = self._load()
            cancelled = 0
            running = False
            for j in jobs:
                if j["status"] == QUEUED:
                    j.update(status=FAILED, read=True,
                             finished=int(time.time()),
                             summary="cancelled", detail="cancelled by voice")
                    cancelled += 1
                elif j["status"] == RUNNING:
                    running = True
            self._save(jobs)
        return cancelled, running

    def unread(self):
        with self._lock:
            return [j for j in self._load()
                    if j["status"] in (DONE, FAILED) and not j.get("read")]

    def latest_result(self):
        """Newest finished job BY COMPLETION TIME, unread first - the 'what
        did you find' answer. File order won't do: _save regroups live-then-
        done, so a queued-later job that finished last can sit earlier in the
        file. Does NOT mark read; the caller does once it was spoken."""
        with self._lock:
            done = [j for j in self._load() if j["status"] in (DONE, FAILED)]
        if not done:
            return None
        done.sort(key=lambda j: j.get("finished", 0))
        unread = [j for j in done if not j.get("read")]
        return (unread or done)[-1]

    def mark_read(self, job_id):
        self._update(job_id, read=True)

    def for_context(self):
        """Recent finished jobs, oldest first - what the assistant should
        already know when you follow up on an announcement. Read/unread is
        irrelevant here: hearing a result is exactly when you're most likely
        to ask about it."""
        now = time.time()
        with self._lock:
            done = [j for j in self._load()
                    if j["status"] in (DONE, FAILED)
                    and now - j.get("finished", 0) < CONTEXT_AGE_S]
        done.sort(key=lambda j: j.get("finished", 0))
        return done[-CONTEXT_JOBS:]

    def status_line(self):
        """One spoken sentence about what's in flight."""
        with self._lock:
            jobs = self._load()
        active = [j for j in jobs if j["status"] in (QUEUED, RUNNING)]
        if not active:
            return None
        return (f"{len(active)} task{'s' if len(active) > 1 else ''} still "
                "in flight")
