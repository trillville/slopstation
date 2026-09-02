---
name: sentry-traces
description: Read agent traces from Sentry - what the voice assistant actually heard, said, which tools it called with what arguments, how long each stage took, and how many tokens it used. Use when asked why the assistant answered a certain way, which tool it reached for (or failed to), why a reply was slow, what a conversation cost or how many tokens it used, whether the grammar or the LLM handled something, or to review agent behaviour and conversation quality. Complements the sentry-logs skill: that one is for ops (launches, crashes, liveness), this one is for what the model did. This is the CURRENT trace store; the langfuse-traces skill is the one being retired.
---

# Reading the agent's traces

The voice pipeline ships its spans to Sentry over OTLP. Pipecat builds the
tree; `k15/agent/telemetry/tracing.py` is only the plumbing.

```text
conversation "voice session"   <- session id, user, env
└── turn                        <- one exchange (includes human thinking time)
    ├── stt   what Flux transcribed
    ├── llm   model, prompt/completion tokens, TTFB
    └── tts   voice, character count, TTFB
```

```bash
python .claude/skills/sentry-traces/query.py conversations --since 24h
python .claude/skills/sentry-traces/query.py session c32ec7 --io
python .claude/skills/sentry-traces/query.py tools --since 7d
```

| Command | What it answers |
|---|---|
| `conversations --since 24h` | one row per voice session, newest activity first |
| `session <id>` | every span in one conversation, oldest first |
| `trace <traceId>` | one trace as a timeline |
| `trace <id> --io` / `session <id> --io` | the same, plus prompts, completions and tool arguments |
| `tools --since 7d` | tool calls grouped by name, with average duration |

**During the migration** the `langfuse-traces` skill still works and holds
anything from before the cutover. Prefer this one; fall back there for older
sessions.

## Joining to the logs

The conversation id is our **`session`** — the same value in the JSONL:

```bash
python .claude/skills/sentry-logs/query.py --session c32ec7 --since 24h
```

Sentry says what the model did; the logs say what the system did around it —
the dispatch, the launch, the earcons, the errors.

## How to answer well

1. **Start with `conversations`** to find the one in question, then `session`
   it. Do not dump every span.
2. **Attribute latency to a stage** — "6 s: 3.8 s on the model, 2.0 s on TTS",
   not "it was slow".
3. **Read `--io` before judging behaviour.** The assistant's context carries
   the whole game catalog and the carried turns; a strange answer is usually a
   context problem. `--io` shows what the model **said**; the
   `gen_ai.execute_tool` spans show what it **called**, with arguments.
4. **Watch the prompt token count.** ~60k per turn is normal (catalog in
   context) and prompt caching keeps the cost near zero. A sudden jump means
   the catalog or the carry grew.
5. **`gate_match` never reaches the traces.** Grammar-matched commands are
   handled deterministically without an LLM call, so a command missing from
   the traces was probably handled by the grammar — confirm in the logs with
   `--event gate_match`, and treat that as the *good* outcome.

## Gotchas

- **Pipecat's `llm` span never carries the tool call**, so an empty output on
  a span that burned output tokens is the tell that the model emitted one.
  `function_schemas` wraps every tool and calls `tracing.tool_span`, so each
  also shows as a **`gen_ai.execute_tool`** span with its arguments and
  result. Same for the `tool_call` log event (with `turn`), which outlives
  Sentry's span retention.
- **The catalog is not in the traced messages.** The ~60k-token prompt rides
  the Responses API `instructions` parameter, which Pipecat does not put in
  `gen_ai.input.messages`. Measured span payloads are under 4 kB, so nothing
  is truncated — but it also means `--io` does not show you the catalog. Read
  it from `library.catalog_lines()` if you need it.
- **The `--text` REPL and the chord lane are not traced** — those live in the
  logs. Background jobs are traced.
- **Retention is 30 days full-fidelity**, then a downsampled tier meant for
  aggregate trends, not for retrieving one conversation. Anything older lives
  in `k15\state\traces\<stamp>-voice.json` (the local mirror, 14 days, whole
  message history) and in the logs.
- **Spans flush on a batch timer**, so give a session ~30 s after it ends
  before concluding a trace is missing.
- **Attribute names come from Pipecat**, not from us — only `session.id`,
  `gen_ai.conversation.id`, `env` and `couch.turn` are set by this repo. If a
  column is empty, use `--json` to see what the API actually returned rather
  than assuming the data is missing.
- **Sentry has no evals, prompt management, datasets or playground.** If a
  question needs those, it needs a different tool; this skill answers "what
  happened", not "was it good".
