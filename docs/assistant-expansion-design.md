# Assistant expansion (Project D) — v1 spec

**Status: SPEC, decisions locked 2026-08-11.** Product of the harness research
pass (three deep-dives: mature harnesses, OSS landscape, MCP/hybrid patterns).
Verdict recorded here once: **keep the harness, grow it** — no surveyed harness
(Claude Agent SDK/Code/Desktop/Cowork, ChatGPT, OpenClaw, Home Assistant, OSS
field) supplies a Windows wake-word voice loop, both consumer voice modes are
closed to custom MCP tools, and the industry converged on our exact
deterministic-first + LLM-fallback design (HA's `prefer_local_intents`). The
mature harnesses enter as **components inside our architecture**: server-side
search in the lane, MCP as the tool boundary, agent CLIs as async workers.

Three additions, zero rework of proven lanes:

| | What | Ships as |
|---|---|---|
| **D1** | In-lane web search, both provider backends, one config knob | an evening |
| **D2** | MCP server over the existing tool surface; local stdio + SSH forced-command remote | a weekend day |
| **D3** | Tier-3 async worker lane: `claude -p` + `codex exec` adapters, research-only, proactive spoken results | a weekend |

**Deferred:** hand-rolled memory (`remember` tool + facts file in the system
tail). Designed, not scheduled; nothing in D1–D3 blocks it.

## Decisions locked (user calls, 2026-08-11)

| # | Decision | Call |
|---|---|---|
| 1 | Remote MCP access | **SSH forced-command stdio** from the desktop — mirrors the Dispatch.ps1 pattern; key + forced command is the entire surface |
| 2 | Job-result delivery | **Proactive spoken announcement** on completion — earcon, then speak, even mid-movie. Defers only while a voice session is active (audio contention), announces at session close |
| 3 | Worker scope v1 | **Research-only** — web search/fetch + read-only library tools; no launches, no TV, no Bash. Widening is config, not redesign |
| 4 | Worker adapters v1 | **Both** — ClaudeWorker (`claude -p`, Claude subscription) and CodexWorker (`codex exec`, ChatGPT plan), config-switched like `assistantProvider` |

## Invariants (unchanged, normative)

- The voice runtime is untouched: wake loop, pre-roll, Flux, GrammarGate,
  per-session pipeline, hold/linger, earcon vocabulary.
- The one rule, the session lock as arbiter, teardown-wins — all unchanged.
- **`dispatch.py` stays the single side-effect chokepoint.** The MCP server and
  the workers are *clients* of it, never bypasses. Workers never see SSH keys.
- No new secrets in the repo. Agent CLIs authenticate on-machine
  (`claude login` / `codex login`), outside `secrets.json`.
- Provider-swappability is a hard requirement at every new seam: per-backend
  native tools behind one knob (D1), provider-neutral MCP (D2), adapter pair
  behind `workerProvider` (D3). Nothing chains a lane to a vendor.

---

## D1 — In-lane web search

Quick facts ("is the Elden Ring DLC out yet?") stay conversational; only deep
work goes async. Each backend appends **its provider's native server-side
search tool** — the model decides when to search, so unsearched turns keep
today's latency.

**Config** (`config.json` `voice`): `assistantWebSearch` (bool, ships
`false`, flipped after the drill), `assistantSearchMaxUses` (int, 2),
`location` (`{city, region, country, timezone}`, optional, feeds both
providers' `user_location`).

**`assistant.py`:**
- `AnthropicBackend.turn`: when enabled, append
  `{"type": "web_search_20250305", "name": "web_search", "max_uses": N,
  "user_location": …}` to the tools array. Handle `stop_reason: "pause_turn"`
  (re-send the assistant content as-is and continue). Server-tool blocks
  already survive verbatim in history (we append `resp.content` wholesale).
- `OpenAIBackend.turn`: append `{"type": "web_search",
  "search_context_size": "low", "user_location": …}`. `web_search_call` items
  appear in `resp.output`; no client action. (Note for posterity: the claim
  that web search and function tools are mutually exclusive is a kie.ai proxy
  limitation, not the OpenAI API — the official gpt-5.6-luna model page lists
  both.)
- System prompt: one added rule — search only for facts beyond the catalog
  (releases, news, prices); spoken answers stay ≤2 sentences; never read URLs
  aloud.

**`voice_agent.py` (prod pipeline):** the OpenAI lane runs through our
`OpenAIResponsesHttpLLMService`, so the tools entry and a **"checking" cue**
(one soft earcon on the first `web_search_call` event per turn — tone
placeholder-tunable per the earcon convention) are our code. The Anthropic
lane via Pipecat's `AnthropicLLMService` may need a subclass for server-tool
passthrough — deferred until a provider swap back (risk table).

**Cost/latency:** ~$0.01/search both providers + search-content tokens;
searched turns 3–6 s (cue covers it), unsearched turns unchanged.

**Tests (blind):** tools-array composition per provider × knob; `pause_turn`
handling from a canned fixture; cue emission logic.
**Drills:** searched question → cue → answer < 6 s; unsearched question at
current latency (log shows no search item); "volume up" mid-answer still
Tier-1; REPL A/B (`--provider anthropic|openai`) both search.

---

## D2 — MCP server

One new file, `k15/voice/mcp_server.py`, official `mcp` Python SDK (FastMCP),
**stdio transport only** — the stable subset of the spec (tools-only, no
sessions), immune to the 2026-07-28 stateless-core migration.

**Tools = the existing surface, one source of truth.** The server imports
`assistant.tool_impls` / `TOOL_DEFS` and re-presents them — plus one addition
that in-context clients don't need but external harnesses do:
`get_catalog()` (read-only, returns `library.catalog_lines()`).

**Profiles** (`--profile full|research`):
- `full` (desktop, default): everything — launch, control, now-playing,
  details, catalog. The desk user is a present user.
- `research` (workers): read-only — `get_catalog`, `get_game_details`,
  `get_now_playing`. No side-effect tools exist in the listing at all.

`--dry-run` mirrors the REPL convention for bench use.

**Concurrency:** safe by construction — Ex-Link sends have port-contention
retry in `cglib`, session arbitration lives in the lock, dispatch verbs are
BUSY-truthful. An MCP client is just another dispatch caller.

**Remote access (decision 1):** K15 sshd, one keypair from the desktop,
`authorized_keys` entry with forced command
`<venv-python> <repo>\k15\voice\mcp_server.py --profile full` plus
`no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding`. Desktop
side: an ssh-config `Host slopstation-mcp` alias, and an `.mcp.json` entry
`{"command": "ssh", "args": ["slopstation-mcp"]}`. Server lifecycle =
connection lifecycle; nothing resident. This is a **new, deliberate inbound
surface on the K15** (it already hosts VirtualHere): LAN-only, key-gated,
forced-command-constrained — the same posture as the gaming PC's
`administrators_authorized_keys`.

**Doctor rows:** `mcp` importable; server self-test (`--selftest` spawns,
lists tools, checks profile filtering); WARN if sshd absent when the remote
path is configured.

**Tests (blind):** tool listing matches TOOL_DEFS per profile; research
profile provably excludes side-effect tools; dry-run never dispatches.
**Drills:** from desktop Claude Code — "what's in my library" (catalog flows);
"launch Armored Core VI" (full chain fires: session, READY, input switch);
research profile refuses a launch.

---

## D3 — Tier-3 async worker lane

"Work on this and get back to me." Latency-free lane, so provider-agnosticism
is nearly free: both vendors ship their harness as a headless,
subscription-billed CLI, and both consume the same MCP server from config.

**`k15/voice/jobs.py` — JobStore.** `state/jobs.json` (keep last 10, with
read/unread), single worker thread, queue cap 3, `workerTimeoutS` (600).
Job: `{id, task, status: QUEUED|RUNNING|DONE|FAILED, summary, detail,
provider, created}`. **Reconciler on agent startup** (house rule: every piece
of distributed state has one): RUNNING jobs from a dead process → FAILED
"agent restarted", announced once.

**`k15/voice/workers.py` — the adapter pair.** Contract:
`run(task) -> {ok, summary, detail}` where `summary` is ≤2 sentences in
spoken register (the worker prompt demands this shape as JSON; parse fallback
= whole text as `detail`, first sentence as `summary`).
- `ClaudeWorker`: `claude -p <prompt> --output-format json --max-turns N
  --allowedTools "WebSearch,WebFetch" --mcp-config <research-profile>`.
- `CodexWorker`: `codex exec --json` with the research-profile MCP server in
  `~/.codex/config.toml`; exact sandbox/search flags pinned at build time
  (they churn; the adapter boundary is what keeps that churn internal).
- Config: `workerProvider` (`claude`), `workerModel` (optional passthrough),
  `workerTimeoutS`. Swap = config string, same as `assistantProvider`.

**Voice surface:**
- New `TOOL_DEFS` entry `background_task(task)` — assistant lane acks
  "I'll look into it and let you know."
- Tier-1 grammar: "what did you find / any updates" → speak latest summary
  (no LLM); "give me the details" → speak `detail`; "cancel the task".
- **Proactive announcement (decision 2):** on completion — soft distinct
  earcon, then the summary, spoken immediately, movies included. One-shot TTS
  outside any session: Aura-2 REST synth → existing output device (Kokoro
  path as offline fallback). Sole gate: an **active session defers the
  announcement to session close** (the pipeline owns the speaker); DORMANT
  announces at once. Unread results also get a one-line mention at next
  session open.

**Worker scope (decision 3):** research-only. `--allowedTools` +
research-profile MCP is the enforcement, not the prompt. No Bash, no
side-effect tools; the worker cannot touch the TV, the session, or the PC.

**Doctor rows:** CLI presence + version for both adapters; credential
presence (file check, not a billed probe); jobs.json readable; orphaned
RUNNING check.

**Tests (blind):** JobStore lifecycle + reconciler; adapter parsing from
canned `claude -p` / `codex exec` JSON fixtures; announcement gating
(active session → deferred); grammar additions.
**Drills:** research request → immediate ack → announcement lands mid-movie;
"what did you find"; kill the agent mid-job → restart → truthful FAILED at
reconcile; pull the network mid-job → FAILED with a plain spoken reason.

---

## Config schema deltas (`config.example.json` `voice`)

```
"assistantWebSearch": false,
"assistantSearchMaxUses": 2,
"location": {"city": "", "region": "", "country": "US", "timezone": ""},
"workerProvider": "claude",
"workerModel": "",
"workerTimeoutS": 600
```

No `secrets.json` changes.

## Deploy list

**K15** (in order): `git pull` → venv `pip install mcp` → install + auth both
CLIs (`npm i -g @anthropic-ai/claude-code && claude login`;
`npm i -g @openai/codex && codex login`) → sshd one-time setup + desktop key +
forced-command entry (commands to be recorded here at build time, guide-style)
→ config.json new keys → `Start-K15.bat` (bounces the agent onto new code).
**Desktop:** ssh-config `Host slopstation-mcp` alias; `.mcp.json` entry.
**Gaming PC:** nothing.

## Risks

| Risk | Mitigation |
|---|---|
| Pipecat `AnthropicLLMService` may not pass server-side tools through | Active lane is our own `OpenAIResponsesHttpLLMService`; Anthropic in-lane search deferred to a provider swap, REPL covers it meanwhile |
| `codex exec` flag/JSON churn; Codex-on-Windows still "experimental" | Adapter isolates it; ClaudeWorker is the default; Codex failure = one FAILED job, never a crashed lane |
| Announcement audio vs. pipeline contention | Session-active gate; one-shot TTS only in DORMANT/LINGER; wake loop owns the mic, never the speaker |
| Search latency spikes (multi-search agentic turns) | `max_uses` cap, `search_context_size: low`, checking cue; knob ships off until drilled |
| Heavy CLIs on the appliance | Workers are short-lived subprocesses of the agent; crash = FAILED job; supervisors and the chord lane can't be touched by them |
| New K15 inbound surface (sshd) | Key + forced command + no-pty is the whole surface, LAN-only — the proven gaming-PC posture, pointed the other way |
| MCP spec churn | Official SDK, stdio-only, tools-only — the stable subset; 12-month deprecation policy upstream |

## Docs to touch at build time

README layout table (+`mcp_server.py`, `jobs.py`, `workers.py`),
voice-testing.md (new drill sections D1–D3), troubleshooting.md (search lane,
MCP-over-ssh, worker lane symptom rows), this file's deploy commands filled in
with the exact registrations, assumptions-ledger rows for every judgment call
made during the build.
