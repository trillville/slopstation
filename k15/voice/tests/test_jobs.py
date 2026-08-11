"""Blind test (Project D2): the worker lane minus real CLIs - reply parsing
(contract JSON, fences, prose fallback), adapter argv/extract shapes from
canned fixtures, JobStore lifecycle on a temp state file (enqueue -> run ->
DONE, cap, cancel, unread flow), the restart reconciler, and the announcer's
defer/abort gates with playback stubbed. Run:
    .venv\\Scripts\\python tests\\test_jobs.py
"""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import announce
import jobs as jobs_mod
import workers


def wait_for(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def main():
    logs = []
    log = lambda m: logs.append(m)  # noqa: E731

    # -- parse_reply: contract, fenced, prose fallback ------------------------
    r = workers.parse_reply('{"summary": "Two picks.", "detail": "Long text."}')
    assert r == {"summary": "Two picks.", "detail": "Long text."}
    r = workers.parse_reply('Here you go:\n```json\n{"summary": "S.", '
                            '"detail": "D."}\n```\nDone!')
    assert r["summary"] == "S." and r["detail"] == "D."
    r = workers.parse_reply("First sentence here. Second sentence.")
    assert r["summary"] == "First sentence here." and "Second" in r["detail"]
    r = workers.parse_reply('{"detail": "no summary key"}')      # bad contract
    assert "no summary key" in r["detail"]                       # fallback path
    r = workers.parse_reply("")
    assert r["summary"] and r["detail"]                          # never empty
    print("  parse_reply: contract, fences, prose fallback, never empty")

    # -- adapter argv + extract shapes (no CLI spawned) -----------------------
    cw = workers.ClaudeWorker(model="")
    cw.path = r"C:\x\claude.cmd"
    argv = cw._argv()
    assert argv[:3] == ["cmd.exe", "/c", r"C:\x\claude.cmd"]     # .cmd shim
    assert "-p" in argv and "--output-format" in argv and "json" in argv
    assert "--model" not in argv                 # empty = the CLI's own
    # One vocabulary across both lanes, and a model key per vendor - so a
    # claude alias can never reach codex (invalid-model error) and neither
    # lane hides a default this file would have to be read to discover.
    assert set(workers.WORKERS) == set(workers.MODEL_KEY) == {"anthropic",
                                                              "openai"}
    assert workers.WORKERS["anthropic"].exe == "claude"
    assert workers.WORKERS["openai"].exe == "codex"
    assert workers.MODEL_KEY["anthropic"] == "workerModelAnthropic"
    assert not any(a.startswith("TASK") for a in argv)           # prompt=stdin
    assert cw._env() is None                     # no knob -> inherit the CLI's
    cw2 = workers.ClaudeWorker(model="claude-haiku-4-5", effort="high")
    cw2.path = r"C:\x\claude.exe"
    assert cw2._argv()[0].endswith(".exe") and "--model" in cw2._argv()
    # Claude's depth knob is an env var, and the rest of the environment
    # (PATH, the CLI's own credentials) must survive it.
    env = cw2._env()
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "high" and "PATH" in env
    out = json.dumps({"type": "result", "result":
                      '{"summary": "Found it.", "detail": "All of it."}'})
    r = cw._extract(types.SimpleNamespace(stdout=out))
    assert r == {"summary": "Found it.", "detail": "All of it."}
    r = cw._extract(types.SimpleNamespace(stdout="not json at all"))
    assert r["summary"]                                          # fallback

    xw = workers.CodexWorker(effort="high")
    xw.path = r"C:\x\codex.exe"
    argv = xw._argv()
    assert "exec" in argv and "--output-last-message" in argv
    assert argv[-1] == "-"                                       # prompt=stdin
    # Codex carries effort as a TOML -c override (its own default is medium,
    # tuned for interactive work - wrong trade for a latency-free lane).
    assert 'model_reasoning_effort="high"' in argv
    assert xw._env() is None                     # flag-carried, not env
    xw_bare = workers.CodexWorker()
    xw_bare.path = r"C:\x\codex.exe"
    assert not any("model_reasoning_effort" in a for a in xw_bare._argv())

    # Auth failures need a human, so they say so from across the room; every
    # other non-zero exit stays the generic line (with the tail in detail).
    def canned(returncode, stderr):
        w = workers.ClaudeWorker()
        w.path = r"C:\x\claude.exe"
        w._argv = lambda: ["cmd.exe", "/c", "exit", str(returncode)]
        workers.subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=returncode, stdout="", stderr=stderr)
        return w.run("t", 5)
    real_run = workers.subprocess.run
    try:
        r = canned(1, "Error: Not logged in. Run `claude login`.")
        assert "needs a re-login" in r["summary"] and not r["ok"]
        assert "Not logged in" in r["detail"]
        r = canned(2, "Error: rate limit exceeded, try later")
        assert r["summary"] == "the task failed"
    finally:
        workers.subprocess.run = real_run
    workers.WORKER_HOME.mkdir(exist_ok=True)
    xw.LAST.write_text('{"summary": "Codex says.", "detail": "Much."}',
                       encoding="utf-8")
    r = xw._extract(types.SimpleNamespace(stdout=""))
    assert r["summary"] == "Codex says."
    xw.LAST.unlink()
    print("  adapters: argv shapes (.cmd routing, stdin prompt), extract fixtures")

    # -- JobStore on a temp state file ---------------------------------------
    jobs_mod.JOBS_FILE = Path(tempfile.mkdtemp()) / "jobs.json"

    class FakeAdapter:
        exe = "fake"
        def __init__(self):
            self.ran = []
        def run(self, task, timeout):
            self.ran.append(task)
            if "explode" in task:
                return {"ok": False, "summary": "the task failed",
                        "detail": "boom"}
            time.sleep(0.05)
            return {"ok": True, "summary": f"did {task}", "detail": "full"}

    done_hook = []
    fake = FakeAdapter()
    store = jobs_mod.JobStore(log, fake, timeout_s=5, on_done=done_hook.append)
    store.start()

    ok, detail = store.enqueue("find coop games")
    assert ok and "queued" in detail
    assert wait_for(lambda: len(done_hook) == 1)
    assert done_hook[0]["status"] == "DONE"
    job = store.latest_result()
    assert job["summary"] == "did find coop games" and not job["read"]
    store.mark_read(job["id"])
    assert store.unread() == [] and store.latest_result()["read"]

    ok, _ = store.enqueue("explode please")
    assert wait_for(lambda: len(done_hook) == 2)
    assert done_hook[1]["status"] == "FAILED"
    assert store.latest_result()["summary"] == "the task failed"
    print("  JobStore: enqueue -> DONE/FAILED, hook fired, unread flow")

    # Cap: fill the queue with a slow adapter, 4th refused truthfully.
    fake.run = lambda task, timeout: (time.sleep(0.5),
                                      {"ok": True, "summary": "s",
                                       "detail": "d"})[1]
    oks = [store.enqueue(f"job {i}")[0] for i in range(4)]
    assert oks[:3] == [True, True, True] and oks[3] is False
    n, running = store.cancel_queued()
    assert n >= 1                                    # queued ones went
    assert wait_for(lambda: store.status_line() is None, timeout=8)
    print("  JobStore: cap refuses the 4th, cancel clears the queue")

    # Reconciler: a RUNNING row from a "dead process" -> FAILED + unread.
    rows = json.loads(jobs_mod.JOBS_FILE.read_text(encoding="utf-8"))
    rows.append({"id": "orphan01", "task": "t", "status": "RUNNING",
                 "provider": "fake", "created": 0, "summary": "",
                 "detail": "", "read": True})
    jobs_mod.JOBS_FILE.write_text(json.dumps(rows), encoding="utf-8")
    assert store.reconcile() == 1
    orphan = [j for j in store.unread() if j["id"] == "orphan01"]
    assert orphan and orphan[0]["status"] == "FAILED"
    print("  reconciler: RUNNING orphan -> FAILED, surfaces as unread")

    # -- announcer gates (synth + playback stubbed) ---------------------------
    voice = {"outputDeviceName": "", "ttsVoice": "aura-2-thalia-en"}
    ann = announce.Announcer(voice, {"deepgramApiKey": "x" * 24}, log)
    ann.jobs = store
    played = []
    ann._play = lambda pcm: played.append(len(pcm)) or True
    announce.synth = lambda text, key, model: b"\x00" * 3200
    target = orphan[0]
    ann.session_active.set()                        # session owns the speaker
    ann.submit(target)
    time.sleep(0.4)
    assert not played, "must defer while a session is active"
    ann.session_active.clear()
    assert wait_for(lambda: played)                 # spoke after session close
    assert wait_for(
        lambda: not any(j["id"] == target["id"] for j in store.unread()))
    ann._play = lambda pcm: False                   # aborted mid-playback
    rows = json.loads(jobs_mod.JOBS_FILE.read_text(encoding="utf-8"))
    for j in rows:
        j["read"] = False
    jobs_mod.JOBS_FILE.write_text(json.dumps(rows), encoding="utf-8")
    ann.submit(store.latest_result())
    time.sleep(0.4)
    assert store.unread(), "an aborted announcement must stay unread"
    print("  announcer: defers for sessions, marks read only after full playback")

    # latest_result orders by COMPLETION time, not file position: _save
    # regroups live-then-done, so a queued-later job that finished last can
    # sit earlier in the file (the audit bug: A then B queued, both done ->
    # file [B, A] -> naive [-1] speaks the older A).
    base = {"task": "t", "status": "DONE", "provider": "fake", "created": 0,
            "detail": "d", "read": True}
    jobs_mod.JOBS_FILE.write_text(json.dumps([
        {**base, "id": "late", "finished": 200, "summary": "newer"},
        {**base, "id": "early", "finished": 100, "summary": "older"},
    ]), encoding="utf-8")
    assert store.latest_result()["summary"] == "newer"
    print("  latest_result: completion-time ordering beats file order")

    print("OK - jobs: parsing, adapters, store lifecycle, reconcile, announcer gates")


if __name__ == "__main__":
    main()
