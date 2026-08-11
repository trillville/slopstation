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

HERE = Path(__file__).resolve().parent
JOBS_FILE = HERE.parent / "state" / "jobs.json"

QUEUED, RUNNING, DONE, FAILED = "QUEUED", "RUNNING", "DONE", "FAILED"
KEEP = 10                       # finished jobs retained in the file
QUEUE_CAP = 3                   # queued-or-running ceiling; beyond = busy


class JobStore:
    """adapter = a workers.*Worker (never None - the caller gates the lane);
    on_done = fn(job dict), called off-thread when a job finishes."""

    def __init__(self, log, adapter, timeout_s, on_done=None):
        self.log = log
        self.adapter = adapter
        self.timeout_s = timeout_s
        self.on_done = on_done
        self._lock = threading.Lock()
        self._kick = threading.Event()

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
                j.update(status=FAILED, read=False,
                         summary="a background task was lost to a restart",
                         detail="the voice agent restarted while this job ran; "
                                "ask again to re-run it")
            self._save(jobs)
        for j in orphans:
            self.log(f"jobs reconcile: {j['id']} RUNNING -> FAILED (restart)")
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
                   "summary": "", "detail": "", "read": True}
            self._save(jobs + [job])
        self.log(f"job {job['id']} queued: {task[:80]}")
        self._kick.set()
        return True, "queued - the result will be announced"

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
                self.log(f"job {job['id']} running ({self.adapter.exe})")
                t0 = time.time()
                r = self.adapter.run(job["task"], self.timeout_s)
                status = DONE if r["ok"] else FAILED
                self._update(job["id"], status=status, read=False,
                             summary=r["summary"], detail=r["detail"])
                self.log(f"job {job['id']} {status} in {time.time() - t0:.0f}s")
                if self.on_done:
                    job.update(status=status, summary=r["summary"],
                               detail=r["detail"])
                    try:
                        self.on_done(job)
                    except Exception as e:
                        self.log(f"job {job['id']} announce hook failed: {e!r}")

    # -- the voice surface ----------------------------------------------------

    def cancel_queued(self):
        """Cancel every QUEUED job (a RUNNING subprocess is not killable
        mid-flight in v1 - the honest answer is it finishes or times out).
        Returns (n_cancelled, running_now)."""
        with self._lock:
            jobs = self._load()
            cancelled = 0
            running = False
            for j in jobs:
                if j["status"] == QUEUED:
                    j.update(status=FAILED, read=True,
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
        """Newest finished job, unread first - the 'what did you find'
        answer. Does NOT mark read; the caller does once it was spoken."""
        with self._lock:
            done = [j for j in self._load() if j["status"] in (DONE, FAILED)]
        if not done:
            return None
        unread = [j for j in done if not j.get("read")]
        return (unread or done)[-1]

    def mark_read(self, job_id):
        self._update(job_id, read=True)

    def status_line(self):
        """One spoken sentence about what's in flight."""
        with self._lock:
            jobs = self._load()
        active = [j for j in jobs if j["status"] in (QUEUED, RUNNING)]
        if not active:
            return None
        return (f"{len(active)} task{'s' if len(active) > 1 else ''} still "
                "in flight")
