"""Background tasks: the job store and its runner. A job is a research
brief handed to an agent CLI (workers.py) that answers minutes later, out
of session. state/jobs.json is the one home for job state, reconciled at
startup. One worker thread, small queue: a job ends DONE or FAILED, never
vanishes.

Read/unread is the announcement contract: a finished job stays unread until
its summary has been HEARD (full playback, or a "what did you find"
retrieval), so an aborted announcement is picked up at the next wake."""
import json
import threading
import time
import uuid

import cglib
import events
import tracing

JOBS_FILE = cglib.STATE / "jobs.json"

QUEUED, RUNNING, DONE, FAILED = "QUEUED", "RUNNING", "DONE", "FAILED"
KEEP = 10                       # finished jobs retained in the file
QUEUE_CAP = 3                   # queued-or-running ceiling; beyond = busy

# What a session's assistant is told about recent background work; bounded
# because it rides in every LLM turn. An unread result must survive until it
# is heard, but a heard one ages out fast - under one flat 6 h window a report
# heard at 18:21 was still opening every session at 20:37 (2026-08-15).
CONTEXT_AGE_S = 6 * 3600        # unread
CONTEXT_READ_AGE_S = 15 * 60    # read (heard, or retrieved by asking)
CONTEXT_JOBS = 2                # only the last couple
CONTEXT_DETAIL_CHARS = 1200     # only the readable head of a long report


class JobStore:
    """adapter = a workers.*Worker (never None - the caller gates the lane);
    on_done = fn(job dict), called off-thread when a job finishes."""

    # The codex lane keeps a shell (the claude lane has none), so --dry-run
    # can't gate a worker the way dispatch.py gates the in-session actions.
    # Advisory only.
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

    # -- the state file (all access under the lock) ---------------------------

    def _load(self):
        try:
            return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, jobs):
        live = [j for j in jobs if j["status"] in (QUEUED, RUNNING)]
        done = [j for j in jobs if j["status"] not in (QUEUED, RUNNING)]
        cglib.write_json(JOBS_FILE, live + done[-KEEP:], indent=1)

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

    def enqueue(self, task, asked=None):
        """-> (ok, spoken detail); busy beyond QUEUE_CAP. `asked` is the user's
        own words (dispatch.Utterance), None on the chord lane and the REPL."""
        with self._lock:
            jobs = self._load()
            active = [j for j in jobs if j["status"] in (QUEUED, RUNNING)]
            if len(active) >= QUEUE_CAP:
                return False, f"{len(active)} tasks are already in flight"
            job = {"id": uuid.uuid4().hex[:8], "task": task, "status": QUEUED,
                   "provider": self.adapter.exe, "created": int(time.time()),
                   "summary": "", "detail": "", "read": True,
                   # The asking span as a W3C traceparent - here is the last
                   # moment it is active; the worker runs on another thread
                   # minutes later. None when tracing is off.
                   "trace": tracing.carrier(),
                   "session": events.current().get("session"),
                   # The user's words beside the brief the model wrote: replay
                   # needs a true user turn and the brief is not one.
                   "asked": asked}
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
                t0 = time.time()
                try:
                    self._run_one(job, t0)
                except Exception as e:
                    # The thread outlives any job: a RUNNING row that never
                    # finishes would otherwise wait for the next restart.
                    self.log.error("job_failed", job=job["id"], status=FAILED,
                                   dur_ms=round((time.time() - t0) * 1000),
                                   session=job.get("session"),
                                   summary="the task crashed", err=repr(e))
                    try:
                        self._update(job["id"], status=FAILED, read=False,
                                     finished=int(time.time()),
                                     summary="the task crashed",
                                     detail=repr(e))
                    except Exception:
                        pass

    def _run_one(self, job, t0):
        self.log("job_running", job=job["id"], provider=self.adapter.exe,
                 dry_run=self.dry_run or None)
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
        """Cancel every QUEUED job. A RUNNING subprocess is not killed; it
        finishes or times out. Returns (n_cancelled, running_now)."""
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
        """Newest finished job by COMPLETION TIME, unread first - the 'what did
        you find' answer. Not file order: _save regroups live-then-done, so a
        job that finished last can sit earlier. Does not mark read."""
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
        """Recent finished jobs, oldest first - what the assistant should know
        when you follow up on an announcement. Read jobs are included but age
        out on the shorter CONTEXT_READ_AGE_S window."""
        now = time.time()
        with self._lock:
            done = [j for j in self._load()
                    if j["status"] in (DONE, FAILED)
                    and now - j.get("finished", 0) < (
                        CONTEXT_READ_AGE_S if j.get("read") else CONTEXT_AGE_S)]
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
