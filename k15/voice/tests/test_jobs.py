"""Blind test: the worker lane minus real CLIs - reply parsing, adapter
argv/extract shapes, JobStore lifecycle on a temp state file, the restart
reconciler, and the announcer gates with playback stubbed. Run:
    .venv\\Scripts\\python tests\\test_jobs.py
"""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

import _bootstrap                               # noqa: F401,E402
from _bootstrap import fresh_state              # noqa: E402

import announce
import cglib
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
    log = cglib.CapturingLog("jobs")
    fresh_state()

    # -- parse_reply: contract, fenced, prose fallback ------------------------
    r = workers.parse_reply('{"summary": "Two picks.", "detail": "Long text."}')
    assert r == {"summary": "Two picks.", "detail": "Long text."}
    r = workers.parse_reply('Here you go:\n```json\n{"summary": "S.", '
                            '"detail": "D."}\n```\nDone!')
    assert r["summary"] == "S." and r["detail"] == "D."
    r = workers.parse_reply("First sentence here. Second sentence.")
    assert r["summary"] == "First sentence here." and "Second" in r["detail"]
    r = workers.parse_reply('{"detail": "no summary key"}')
    assert "no summary key" in r["detail"]                       # fallback path
    r = workers.parse_reply("")
    assert r["summary"] and r["detail"]                          # never empty
    print("  parse_reply: contract, fences, prose fallback, never empty")

    # -- adapter argv + extract shapes (no CLI spawned) -----------------------
    cw = workers.ClaudeWorker(model="")
    cw.path = r"C:\x\claude.cmd"
    argv = cw._argv()
    assert argv[:3] == ["cmd.exe", "/c", r"C:\x\claude.cmd"]     # .cmd shim
    # stream-json: tool calls exist only in the stream; plain json leaves just
    # the final text. --verbose is required alongside it.
    assert "-p" in argv and "--output-format" in argv
    assert "stream-json" in argv and "--verbose" in argv
    cw.stream = False                            # the usage-error fallback
    assert "json" in cw._argv() and "stream-json" not in cw._argv()
    cw.stream = True
    assert "--model" not in argv                 # empty = the CLI's own
    # One vocabulary across both lanes, a model key per vendor.
    assert set(workers.WORKERS) == set(workers.WORKER_MODEL_KEY) == {"anthropic",
                                                              "openai"}
    assert workers.WORKERS["anthropic"].exe == "claude"
    assert workers.WORKERS["openai"].exe == "codex"
    assert workers.WORKER_MODEL_KEY["anthropic"] == "workerModelAnthropic"
    assert not any(a.startswith("TASK") for a in argv)           # prompt=stdin
    assert cw._env() is None                     # no knob -> inherit the CLI's
    cw2 = workers.ClaudeWorker(model="claude-haiku-4-5", effort="high")
    cw2.path = r"C:\x\claude.exe"
    assert cw2._argv()[0].endswith(".exe") and "--model" in cw2._argv()
    # Claude's depth knob is an env var; PATH/credentials must survive it.
    env = cw2._env()
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "high" and "PATH" in env
    # Single-object stdout: what the fallback produces and older CLIs emit.
    out = json.dumps({"type": "result", "result":
                      '{"summary": "Found it.", "detail": "All of it."}'})
    r = cw._extract(types.SimpleNamespace(stdout=out))
    assert r["summary"] == "Found it." and r["detail"] == "All of it."
    r = cw._extract(types.SimpleNamespace(stdout="not json at all"))
    assert r["summary"]

    # -- the stream: tool calls, their results, and the discarded metadata ----
    # Field names verified against the real CLI (2026-08-12); not published
    # anywhere, so this is where they are pinned.
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "t1", "name": "WebSearch",
             "input": {"query": "couch co-op 2026"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "ten results"}]}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "WebFetch",
             "input": {"url": "https://example.com/a"}}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": '{"summary": "Three picks.", "detail": "The long form."}',
         "total_cost_usd": 0.073279, "num_turns": 4, "stop_reason": "end_turn",
         "session_id": "2c18b2b6", "duration_api_ms": 2514,
         # server_tool_use counts the API's own server-executed searches; this
         # CLI's WebSearch/WebFetch are harness tools, so it reports zeros while
         # the stream carries real tool_use blocks (2026-08-14).
         "usage": {"input_tokens": 2, "output_tokens": 12,
                   "cache_read_input_tokens": 20938,
                   "server_tool_use": {"web_search_requests": 0,
                                       "web_fetch_requests": 0}},
         "permission_denials": [{"tool": "Bash"}],
         "modelUsage": {"claude-opus-5[1m]": {"canonicalModel": "claude-opus-5",
                                              "costUSD": 0.073279}}},
    ])
    r = cw._extract(types.SimpleNamespace(stdout=stream))
    assert r["summary"] == "Three picks." and r["detail"] == "The long form."
    assert [s["tool"] for s in r["steps"]] == ["WebSearch", "WebFetch"]
    assert r["steps"][0]["input"] == "couch co-op 2026"
    assert r["steps"][0]["result"] == "ten results"               # joined back
    assert r["steps"][1]["input"] == "https://example.com/a"
    m = r["meta"]
    assert m["cost_usd"] == 0.073279 and m["turns"] == 4
    assert m["web_searches"] == 1 and m["web_fetches"] == 1   # from the steps
    assert m["denials"] == 1 and m["model"] == "claude-opus-5"
    assert m["cache_read_tokens"] == 20938 and m["cli_session"] == "2c18b2b6"

    # Junk lines, a half-written line and an unknown event type cost tool spans
    # at most, never the answer.
    messy = ("banner text\n" + stream.splitlines()[1] + "\n"
             '{"type": "mystery", "whatever": 1}\n{"type": "assist\n'
             + stream.splitlines()[-1])
    r = cw._extract(types.SimpleNamespace(stdout=messy))
    assert r["summary"] == "Three picks." and len(r["steps"]) == 1

    xw = workers.CodexWorker(effort="high")
    xw.path = r"C:\x\codex.exe"
    argv = xw._argv()
    assert "exec" in argv and "--output-last-message" in argv
    assert argv[-1] == "-"                                       # prompt=stdin
    # Codex carries effort as a TOML -c override, not an env var; its own
    # default is medium.
    assert 'model_reasoning_effort="high"' in argv
    assert xw._env() is None                     # flag-carried, not env
    xw_bare = workers.CodexWorker()
    xw_bare.path = r"C:\x\codex.exe"
    assert not any("model_reasoning_effort" in a for a in xw_bare._argv())

    # Auth failures name themselves; other non-zero exits stay generic.
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

    # -- JobStore on the tmp state file (fresh_state) --------------------------

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

    # --dry-run can't gate a worker's shell, so the task text carries the
    # notice (advisory, and logged).
    dry = jobs_mod.JobStore(log, fake, timeout_s=5, dry_run=True)
    assert dry._task_text({"task": "x"}) == jobs_mod.JobStore.DRY_NOTE + "x"
    assert store._task_text({"task": "x"}) == "x"
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

    fake.run = lambda task, timeout: (time.sleep(0.5),
                                      {"ok": True, "summary": "s",
                                       "detail": "d"})[1]
    oks = [store.enqueue(f"job {i}")[0] for i in range(4)]
    assert oks[:3] == [True, True, True] and oks[3] is False
    n, running = store.cancel_queued()
    assert n >= 1                                    # queued ones went
    assert wait_for(lambda: store.status_line() is None, timeout=8)
    print("  JobStore: cap refuses the 4th, cancel clears the queue")

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
    voice = {"outputDeviceName": "", "ttsVoice": "aura-2-thalia-en",
             "followUpAfterAnnounce": True}
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
    assert wait_for(lambda: played)
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
    # speak() is the out-of-session path --announce-test rehearses.
    ann._play = lambda pcm: played.append(len(pcm)) or True
    played.clear()
    assert ann.speak("bench line") and played

    ann.follow_up.clear()
    ann._play = lambda pcm: False
    rows = json.loads(jobs_mod.JOBS_FILE.read_text(encoding="utf-8"))
    for j in rows:
        j["read"] = False
    jobs_mod.JOBS_FILE.write_text(json.dumps(rows), encoding="utf-8")
    ann.submit(store.latest_result())
    time.sleep(0.4)
    assert not ann.follow_up.is_set(), "cut-short bulletin must not open a mic"
    ann._play = lambda pcm: played.append(len(pcm)) or True
    ann.submit(store.latest_result())
    assert wait_for(ann.follow_up.is_set), "full bulletin should open the mic"
    print("  announcer: follow-up window only after a bulletin played in full")

    # Job results ride into the assistant as prior conversation.
    import session_runtime
    # A brief attributed to the user is a standing instruction for
    # CONTEXT_AGE_S, so the model's words must never be replayed as the user's.
    pre, post = session_runtime.job_messages(store, 0)       # 0 = all post
    msgs = pre + post
    assert msgs and len(msgs) <= 2 * jobs_mod.CONTEXT_JOBS
    brief = store.for_context()[0]["task"]
    for m in msgs:
        if m["role"] == "user":
            assert brief not in m["content"], \
                "the model's own brief is being replayed as the user's words"
    assert session_runtime.job_messages(None, 0) == ([], [])  # worker lane off

    # With a transcript: quotes the person. `asked` is an argument (dispatch's
    # utterance snapshot), not store state.
    store.enqueue("Research couch co-op titles, excluding owned games.",
                  asked="find me some couch co-op games")
    job = [j for j in store._load() if j["status"] == jobs_mod.QUEUED][-1]
    store._update(job["id"], status=jobs_mod.DONE, read=True,
                  finished=int(time.time()), summary="Found three.",
                  detail="The long form.")
    pre, post = session_runtime.job_messages(store, 0)
    msgs = pre + post
    pair = [m for m in msgs if m["role"] in ("user", "assistant")][-2:]
    assert pair[0] == {"role": "user",
                       "content": "find me some couch co-op games"}, pair
    assert "Found three." in pair[1]["content"]

    # Without one (chord lane, REPL, or a record predating `asked`): stated as
    # history, not as something the user said.
    store.enqueue("Some brief nobody spoke aloud.")
    job = [j for j in store._load() if j["status"] == jobs_mod.QUEUED][-1]
    store._update(job["id"], status=jobs_mod.DONE, read=True,
                  finished=int(time.time()), summary="Done.", detail="")
    pre, post = session_runtime.job_messages(store, 0)
    msgs = pre + post
    assert not any("Some brief nobody spoke aloud." in m["content"]
                   for m in msgs if m["role"] == "user")
    assert any(m["role"] == "system" and "Some brief nobody spoke aloud."
               in m["content"] for m in msgs), msgs
    print("  job_messages: the user is quoted, the model's brief never is")
    print("  announcer: defers for sessions, marks read only after full playback")

    # Clock order: a job finished BEFORE the carry snapshot seeds ahead of the
    # carried turns, one AFTER seeds behind them - everything-first made the
    # model deny a result it had delivered (2026-08-15).
    t_now = time.time()
    seed_base = {"task": "t", "status": jobs_mod.DONE, "provider": "fake",
                 "created": 0, "read": True}
    jobs_mod.JOBS_FILE.write_text(json.dumps([
        {**seed_base, "id": "j-old", "finished": t_now - 10,
         "summary": "Old answer.", "detail": "", "asked": "the old ask"},
        {**seed_base, "id": "j-new", "finished": t_now - 2,
         "summary": "New answer.", "detail": "word " * 400,
         "asked": "the new ask"},
    ]), encoding="utf-8")
    pre, post = session_runtime.job_messages(store, t_now - 5)
    assert any("Old answer." in m["content"] for m in pre), pre
    assert any("New answer." in m["content"] for m in post), post
    said = post[-1]["content"]
    assert said.endswith("word [truncated]"), said[-40:]
    assert len(said) < len("word " * 400)
    print("  job_messages: clock-ordered against the carry; cuts on a word")

    # A read result ages out of context fast; an unread one holds the full
    # window, so "what did you find" still works hours later.
    stale = t_now - jobs_mod.CONTEXT_READ_AGE_S - 60
    jobs_mod.JOBS_FILE.write_text(json.dumps([
        {**seed_base, "id": "j-heard", "finished": stale,
         "summary": "Heard.", "detail": "", "asked": "a?"},
        {**seed_base, "id": "j-unheard", "read": False, "finished": stale,
         "summary": "Unheard.", "detail": "", "asked": "b?"},
    ]), encoding="utf-8")
    got = [j["summary"] for j in store.for_context()]
    assert got == ["Unheard."], got
    print("  for_context: heard results age out fast, unheard ones hold")

    # latest_result orders by completion time, not file position: _save
    # regroups live-then-done, so the newer row can sit earlier in the file.
    base = {"task": "t", "status": "DONE", "provider": "fake", "created": 0,
            "detail": "d", "read": True}
    jobs_mod.JOBS_FILE.write_text(json.dumps([
        {**base, "id": "late", "finished": 200, "summary": "newer"},
        {**base, "id": "early", "finished": 100, "summary": "older"},
    ]), encoding="utf-8")
    assert store.latest_result()["summary"] == "newer"
    print("  latest_result: completion-time ordering beats file order")

    # -- an adapter that raises: the thread survives, the job fails out loud ----
    class Boom:
        exe = "boom"

        def run(self, task, timeout):
            raise RuntimeError("adapter exploded")
    crash_log = cglib.CapturingLog("jobs")
    crashy = jobs_mod.JobStore(crash_log, Boom(), timeout_s=5)
    crashy.start()
    assert crashy.enqueue("crash me")[0]
    for _ in range(200):
        if any(j["task"] == "crash me" and j["status"] == "FAILED"
               for j in crashy.unread()):
            break
        time.sleep(0.02)
    row = next(j for j in crashy.unread() if j["task"] == "crash me")
    assert row["status"] == "FAILED" and "exploded" in row["detail"], row
    failed = crash_log.find("job_failed")
    assert failed and "exploded" in failed[0]["err"], failed
    assert failed[0]["summary"] == "the task crashed" and failed[0]["level"] == "error"
    assert crashy.enqueue("still alive")[0]          # the worker thread lives
    print("  crash: a raising adapter -> FAILED row, job_failed err=, thread survives")

    print("OK - jobs: parsing, adapters, store lifecycle, reconcile, announcer gates, "
          "crash path")


if __name__ == "__main__":
    main()
