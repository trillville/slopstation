---
name: langfuse-traces
description: Read agent traces from Langfuse - what the voice assistant actually heard, said, how long each stage took, and what it cost. Use when asked why the assistant answered a certain way, why a reply was slow, what a conversation cost or how many tokens it used, whether the grammar or the LLM handled something, or to review agent behaviour and conversation quality. Complements the grafana-logs skill: that one is for ops (launches, crashes, liveness), this one is for what the model did.
---

# Reading agent traces

Every voice session is one Langfuse trace, emitted by Pipecat's built-in
OpenTelemetry tracing. Query it from the terminal — never send the user to a
browser for something answerable here.

```bash
python .claude/skills/langfuse-traces/query.py conversations --since 24h
python .claude/skills/langfuse-traces/query.py trace <traceId> --io
```

## The shape of a trace

**One trace = one whole conversation**, not one turn. This is Pipecat's model
and it surprises people:

```
conversation  "voice session"      1m01s  $0.004622   ← session id, user, tags
├── turn      9.11s                              one exchange
│   ├── stt      what Flux transcribed
│   ├── llm      model, prompt/completion tokens, TTFB
│   └── tts      voice, character count, TTFB
├── turn      5.50s
└── turn     25.42s
```

Latency on a `turn` includes **the human talking and thinking**. Only the
child spans are system time. A 25 s turn with a 3.8 s `llm` and 2.0 s `tts`
means ~19 s of user, not a slow agent — say so rather than reporting a slow
turn.

A queued background job hangs off the same trace, under the turn that asked
for it, even though it finishes minutes after the conversation ends:

```
conversation
├── turn                     "research couch co-op games I don't own"
│   └── llm                  → called background_task
└── background task   2m14s  couch.job.cost_usd, .turns, .web_searches, .model
    ├── tool: WebSearch      input = the query, output = what came back
    ├── tool: WebFetch       input = the URL
    └── …
```

So **trace latency includes the job's wall-clock** — a 90-second conversation
that queued a 3-minute job reads as ~3 minutes. That is the honest number
(the request wasn't finished until the announcement), but don't report it as
a slow conversation. The `turn` spans are the conversational latency.

Tool spans are point-in-time: the CLI's stream carries no per-tool timings,
so they show **what** the worker called and with what, not how long each
took. `couch.job.denials` > 0 means it tried something the allowlist blocked
— worth reading. A job with `stream_fallback` in its metadata ran on an older
CLI output format and will have no tool spans at all.

## Commands

| | |
|---|---|
| `conversations --since 24h` | one row per voice session: time, trace id, session, latency, cost |
| `trace <traceId>` | the full tree with per-span timing, tokens and cost |
| `trace <traceId> --io` | the same, plus prompts and completions |
| `errors --since 24h` | observations Langfuse marked ERROR |
| `session <sessionId>` | every trace under one session id |

## Joining to the logs

The trace's **session id is the same `session` field** in the Loki JSONL, on
purpose. So a finding in one system is one query away from the other:

```bash
python .claude/skills/grafana-logs/query.py '{service="k15"} | json | session="c32ec7"' --since 24h
```

Use this constantly. Langfuse says what the model did; Loki says what the
system did around it — the dispatch, the launch, the earcons, the errors.
Neither is the whole story.

## How to answer well

1. **Start with `conversations`** to find the one in question, then `trace` it.
   Do not dump every observation.
2. **Attribute latency to a stage.** "The reply took 6 s: 3.8 s waiting on the
   model, 2.0 s on TTS" is useful; "it was slow" is not.
3. **Read `--io` before judging behaviour.** The assistant's context carries
   the whole game catalog and carried turns; a strange answer is usually a
   context problem, not a model problem.
4. **Watch the prompt token count.** Around 60k per turn is normal here —
   catalog-in-context — and prompt caching is what keeps the cost near zero.
   A sudden jump means the catalog or the carry grew.
5. **Cost is per-conversation on the root**, summed from the spans. Sub-cent
   totals are expected; anything near a dollar deserves explanation.
6. **`gate_match` never reaches Langfuse.** Grammar-matched commands are
   handled deterministically without an LLM call, so a command that "did not
   show up in the traces" was probably handled by the grammar — confirm in
   the logs with `event="gate_match"`, and treat that as the *good* outcome.

## Gotchas

- **No `/traces` endpoint.** Langfuse reads traces via
  `/api/public/v2/observations?isRootObservation=true` — the API mirrors the
  UI's "Is Root Observation" filter. The script handles this; do not go
  looking for a traces API.
- **Region.** Keys are per-region. This rig is US
  (`us.cloud.langfuse.com`), set in `k15/config.json` as `langfuseHost`. A
  401 usually means the wrong region, not a wrong key.
- **Traces flush on a batch timer**, so give a session ~30 s after it ends
  before concluding a trace is missing.
- **The `--text` REPL and the chord lane are not traced** — those live in
  the logs. Background jobs ARE, see below.
- **Free tier: 50k units/month**, where a unit is any trace, observation or
  score. A voice session is ~1 + 4×(turn + 3 services) ≈ 17, so ~230/day
  ≈ 7k/mo. Not a constraint at this volume, but do not build polling loops.
  The Hobby plan also caps **history at 30 days**, with 2 users and 2 alerts —
  anything older lives in `state/traces/` and the logs, not here.
- **A cost reading of $0 is a missing price, not a free call.** Langfuse maps
  model name → price from its own list, and prices an unknown model at zero.
  If `couch.job.cost_usd` or a session cost reads zero for a model you know
  costs money, add a custom model definition in project settings.
