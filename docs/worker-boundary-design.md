# Worker boundary — what to do about the research lane

**Status: decision doc for work NOT yet built.** Per CLAUDE.md, delete this once
the chosen option ships and its comments carry the reasoning.

The Tier-3 background worker (`workers.py`) reads untrusted web pages on the K15
— the box holding `secrets.json` and the SSH key to the gaming PC. On 2026-08-14
its claim to be "research-only BY CONSTRUCTION" turned out to be false. This
records what was measured, what is still open, and what each way out costs.

---

## What was actually measured

Everything here is from drilling the live CLI, not from reading docs. The
recurring lesson is that every assumption checked turned out to be wrong.

| Claim | Result |
|---|---|
| `--allowedTools` restricts the worker | **False.** It only auto-approves. `echo` ran with `Bash` absent from the list |
| The surface is ~6 tools | **False — 33**, incl. `PowerShell` (a second shell), `Cron*` (persistence), `Artifact`/`PushNotification`/`SendMessage`/`RemoteTrigger` (exfiltration without a shell), `Agent`/`Workflow` |
| The surface is local | **False.** It inherited the desktop account's MCP connectors: Google Drive read, overwrite, **trash, and public-share** |
| `--disallowedTools` restricts | **True** — the tool leaves the model's list entirely |
| `--mcp-config {} --strict-mcp-config` severs connectors | **True** — surface drops to exactly the six intended |
| `Read` is scoped to `worker_home` | **False.** It read a decoy two directories up |
| `Read(**/path)` deny scoping works | **False.** Denied `Read(**/decoy-probe.txt)`; it read and quoted the file anyway |
| Injecting the catalog can replace file access | **True.** With `Read`/`Glob`/`Grep`/`Write` all denied and the catalog in the prompt, a job still answered correctly ("Hades II, 69.4h played") and researched the web |

### The consequence nobody had noticed

**Today's hardening does not protect `secrets.json`.** `Read` is still granted —
the worker needs it for `library.json` — and `Read` cannot be path-scoped. An
injected instruction can read the file and exfiltrate it inside a `WebSearch`
query. The same tool serves the legitimate need and the attack; they cannot be
separated with CLI flags.

### The bigger one: the assistant's mouth

`session_runtime.job_messages` seeds finished job results into the next
conversation **with `role: "assistant"`** — deliberately, so a follow-up needs no
re-explaining. That means text derived from untrusted web pages is presented to
the assistant as *its own prior words*, the highest-trust position available, in
a context holding `quit_game`, `install_game`, `nav` and `control`.

**No option below fixes this.** Shrinking the worker's tools does not close it —
the channel is the worker's *output*, not its tools. It needs its own mitigation
and should be treated as a separate, arguably higher-priority item.

Partial comfort: `quit_game` is confirm-first, `install_game` validates
ownership, `nav` only accepts validated kinds, and `probe_intent` measures that a
question never becomes an action. The exposure is real but bounded.

---

## The options

### A. Status quo (as hardened 2026-08-14)
Six tools, no MCP, `probe_worker_surface.py` as the canary.
**Leaves `secrets.json` readable.** Denylist needs re-auditing on every CLI
release; the canary makes that loud rather than silent, but it is still a list
someone else grows.

### B. No file tools + injected catalog  ← *cheapest real fix*
Deny `Read`/`Glob`/`Grep`/`Write` too; put the catalog (and `deals.json`) in the
prompt, exactly as the assistant already gets it. **Measured working**, with the
model quoting playtime straight from the injected rows.
- Closes the `secrets.json` path completely — no file tool exists.
- Keeps the CLI's research depth, `WebFetch`, and all its future improvements.
- ~30 lines. No new failure modes.
- Still a denylist against a growing list (canary still required), still
  dependent on CLI/stream-format churn, still no hard cost cap.

### C. Own the loop
An `ApiWorker` beside the existing adapters: Anthropic SDK, server-side web
search, plus the read-only slice of `tool_impls` (`search_store`, `list_games`,
`get_game_details`) and the catalog in the system prompt.
- The surface is ours; it cannot drift, so no denylist and no canary.
- Hard caps on turns and tokens (today a job cost $0.55 with only a timeout as a
  bound).
- Deletes ~100 lines of stream-json churn-handling; unifies "provider" to mean
  model vendor, which also answers the `codex` question.
- **Costs:** Claude Code's harness quality (its system prompt, note-taking,
  self-direction); `WebFetch` depth unless we add a fetch tool, and a fetch tool
  that follows URLs from web content is an SSRF surface needing an allowlist and
  localhost blocking. We take on maintenance the CLI currently gives free.

### D. Low-privilege Windows account + deny ACL
Mechanism-independent: no tool list matters if the OS refuses the read.
- Strongest, and the only one that also contains a future surprise we did not
  anticipate.
- Costs a second local user, a stored password for the scheduled task, and a
  `workers.py` refactor from stdin/stdout to file hand-off — a password added
  while fixing a credential exposure.

---

## Stress-testing C honestly

- **"It's more secure."** Only marginally more than B once file tools are gone.
  B already removes the exfiltration path; C's gain is that the surface cannot
  *drift*, which is a maintenance property more than a security one.
- **"Research quality is the same."** Unproven. The one measured owned-loop
  behaviour is the assistant's own `search_store` answers, which are good but
  not the same task as a three-minute dig. This is the real risk and the reason
  to A/B rather than swap.
- **"It removes the CLI dependency."** True, and that dependency has cost real
  bugs (stream-format churn, flag semantics). But it replaces someone else's
  well-tested loop with ours.
- **"It's ~200 lines."** Plus the ongoing cost of keeping up with model/tool
  conventions the CLI tracks for free.

**Where C genuinely wins:** cost control, provider unification, no canary
treadmill, and library access that is strictly better than file-reading — the
same structured tools the assistant uses instead of raw JSON the worker parses.

---

## Recommendation

1. **Ship B now.** It closes the `secrets.json` path — the only *proven*
   exposure — for ~30 lines and no quality loss (measured). Everything else is
   an improvement on top of a lane that is then genuinely research-only.
2. **Fix the assistant's-mouth channel separately**, because no option above
   touches it. Cheapest credible mitigation: seed job results as a `user`-role
   quote attributed to the worker, or mark them as untrusted data in the text
   itself, so an instruction inside a summary is not read as the assistant's own
   intent. Needs a `probe_intent`-style measurement to confirm the model then
   treats it as data.
3. **Then decide C on evidence.** Run B for a week and read the `tool_call`
   events: if the assistant's fast lane already answers most of what gets asked,
   the worker deserves *narrowing* rather than a rewrite, and C becomes small.
   If the worker earns its keep, build C as a third adapter and A/B it on real
   questions before retiring anything.
4. **Drop `codex`** whichever way this goes. It has none of this hardening, it
   is not the configured provider, and it carries risk for a path nobody uses.

## What gates the next attempt

- C is not worth starting until (2) is done — a hardened worker feeding an
  unhardened channel is the wrong order.
- C needs a bake-off, not a swap: same question, both adapters, compare answers
  and cost before anything is retired.
- If a CLI release ever breaks `--disallowedTools` or `--strict-mcp-config`,
  `probe_worker_surface.py` fails and B is no longer sufficient — that is the
  trigger to stop deferring C or D.
