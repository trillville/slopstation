---
name: langfuse-traces
description: Read agent traces from Langfuse - what the voice assistant actually heard, said, which tools it called with what arguments, how long each stage took, and what it cost. Use when asked why the assistant answered a certain way, which tool it reached for (or failed to), why a reply was slow, what a conversation cost or how many tokens it used, whether the grammar or the LLM handled something, or to review agent behaviour and conversation quality. Complements the grafana-logs skill: that one is for ops (launches, crashes, liveness), this one is for what the model did.
---

# Reading agent traces

Every voice session is one Langfuse trace, emitted by Pipecat's built-in
OpenTelemetry tracing. Query it from the terminal — never send the user to a
browser for something answerable here.

```bash
python .claude/skills/langfuse-traces/query.py conversations --since 24h
python .claude/skills/langfuse-traces/query.py trace <traceId> --io
```

Credentials come from `k15/secrets.json` and the host from `k15/config.json`
(both gitignored). **Worktrees have no copy of either**; the script falls back
to the enclosing checkout's automatically, so a key error means neither
checkout has them (env `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` also works).

## The shape of a trace

**One trace = one whole conversation**, not one turn — Pipecat's model:

```
conversation  "voice session"      1m01s  $0.004622   ← session id, user, tags
├── turn      9.11s                              one exchange
│   ├── stt      what Flux transcribed
│   ├── llm      model, prompt/completion tokens, TTFB
│   └── tts      voice, character count, TTFB
├── turn      5.50s
└── turn     25.42s
```

Latency on a `turn` includes **the human talking and thinking**; only the
child spans are system time. A 25 s turn with a 3.8 s `llm` and 2.0 s `tts`
means ~19 s of user, not a slow agent.

Long-running operations are executed by external authorities and are not
attached to the conversation trace. The submitting tool call remains in its
voice turn; later progress and completion are `operation_*` events in Grafana.
Do not infer operation wall-clock time from Langfuse trace latency.

## Commands

| | |
|---|---|
| `conversations --since 24h` | one row per voice session: time, trace id, session, env, latency, name. No cost — a root observation's cost fields are null; use `trace <id>` |
| `trace <traceId>` | the full tree with per-span timing, tokens and cost |
| `trace <traceId> --io` | the same, plus prompts and completions |
| `errors --since 24h` | observations Langfuse marked ERROR |
| `session <sessionId>` | every trace under one session id |

## Joining to the logs

The trace's **session id is the same `session` field** in the Loki JSONL:

```bash
python .claude/skills/grafana-logs/query.py --session c32ec7 --since 24h
```

`--session` widens to both machines on its own; quoting a raw LogQL selector
here is the PowerShell footgun that skill warns about.

Langfuse says what the model did; Loki says what the system did around it —
the dispatch, the launch, the earcons, the errors.

## How to answer well

1. **Start with `conversations`** to find the one in question, then `trace` it.
   Do not dump every observation.
2. **Attribute latency to a stage** — "6 s: 3.8 s on the model, 2.0 s on TTS",
   not "it was slow".
3. **Read `--io` before judging behaviour.** The assistant's context carries
   the whole game catalog and carried turns; a strange answer is usually a
   context problem. `--io` shows what the model **said**; the `tool: <name>`
   spans show what it **called**, with arguments — read both, and see the
   tool-call gotcha for traces older than 2026-08-14.
4. **Watch the prompt token count.** ~60k per turn is normal (catalog in
   context) and prompt caching keeps the cost near zero. A sudden jump means
   the catalog or the carry grew.
5. **Cost is per-conversation on the root**, summed from the spans. Sub-cent
   totals are expected; anything near a dollar deserves explanation.
6. **`gate_match` never reaches Langfuse.** Grammar-matched commands are
   handled deterministically without an LLM call, so a command missing from
   the traces was probably handled by the grammar — confirm in the logs with
   `event="gate_match"`, and treat that as the *good* outcome.

## Gotchas

- **Pipecat's `llm` span never carries the tool call**, so `output: null` on a
  span that burned output tokens is the tell that the model emitted one.
  `function_schemas` wraps every tool and calls `tracing.tool_span`, so each
  also shows as a **`tool: <name>`** span with its arguments and result,
  parented to that `llm` span. Same for the Loki `event="tool_call"` line
  (with `turn`), which outlives Langfuse's 30-day retention.
  **Traces from before 2026-08-14 have neither** — for those the local mirror
  `k15\state\traces\<stamp>-voice.json` (`messages[].tool_calls`) is the only
  source, and the `trace_saved` log event names the exact file. That mirror
  holds the whole message history, not just the calls.
- **A trace can span several API pages.** `limit=100` is the hard maximum and
  the ROOT observation is often on a later page. `query.py` paginates on
  `meta.totalPages` and prints unreachable spans flat; a hand-rolled call gets
  an empty tree with a healthy-looking span count (2026-08-14).
- **No `/traces` endpoint.** Langfuse reads traces via
  `/api/public/v2/observations?isRootObservation=true`, mirroring the UI's
  "Is Root Observation" filter. The script handles this.
- **Region.** Keys are per-region. This rig is US
  (`us.cloud.langfuse.com`), set in `k15/config.json` as `langfuseHost`. A
  401 usually means the wrong region, not a wrong key.
- **Traces flush on a batch timer**, so give a session ~30 s after it ends
  before concluding a trace is missing.
- **The `--text` REPL and the chord lane are not traced** — those live in
  the logs. Background jobs are traced.
- **Free tier: 50k units/month**, where a unit is any trace, observation or
  score. A voice session is ~1 + 4×(turn + 3 services) ≈ 17, so ~230/day
  ≈ 7k/mo. Not a constraint at this volume, but do not build polling loops.
  The Hobby plan caps **history at 30 days**, with 2 users and 2 alerts —
  anything older lives in `state/traces/` and the logs.
- **A cost reading of $0 is a missing price, not a free call.** Langfuse maps
  model name → price from its own list and prices an unknown model at zero.
  Add a custom model definition in project settings.
