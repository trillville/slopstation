# Voice build — assumptions & open questions ledger

Built blind on the `voice` branch (no live hardware/keys during the build, per
plan). Every judgment call that would normally be a paste-back lands here
instead. **Verdict column is yours** — fill it during the hardware pass; rows
with a failed verdict become the fix list.

Legend: BT = blind-tested during the build (unit/file-driven/local-real) ·
HW = needs the hardware pass · KEY = needs a real key.

| # | Area | Assumption / decision | Why | Resolves by | Verdict |
|---|---|---|---|---|---|
| 1 | keys | Placeholder secrets: any value starting with `PLACEHOLDER` (or the template's `...` forms) disables that lane with a clear startup + doctor message, never a crash | Fail-soft rule | BT (unit) + your eyes at first run | |
| 2 | dispatch | `couch.py` is spawned under the **voice venv's** python (`sys.executable`), not system python — venv now carries pyserial so couch runs fine under it | One interpreter concept per process tree; listener precedent uses its own `sys.executable` | BT (spawn-args unit test) + HW: first voice-started session | |
| 3 | dispatch | "switch to the pc" is **READY-gated** (live `ssh status` check); other inputs switch freely | The one rule: automation never shows a dead input; non-gaming inputs are remote-button equivalent | HW: input drill (expect refusal + hint when no session) | |
| 4 | dispatch | Mute is a **blind toggle**, no software state tracked | Discrete mute on/off doesn't exist in Ex-Link; query support unproven until the probe; tracked state that drifts is worse than none | HW: `exlink.py probe_volume` decides the future | |
| 5 | dispatch | "volume up" = `volumeStep` (5) single-step frames, 50 ms apart, abort on first failed ack | Matches remote-button feel; absolute set exists for jumps | HW: volume drill — tune step count by feel | |
| 6 | earcons | Frequencies/durations chosen by taste (wake 1175 Hz tick, ok 660, busy 2×520, fail 3×330, close soft 440); counts are the contract, tones are placeholder-tunable | Can't audition blind | HW: first listen — retune freely, tests only pin counts | |

| 7 | architecture | **Per-session pipeline**: wake detection runs outside Pipecat (raw PyAudio + openWakeWord loop); each wake builds and runs a fresh `PipelineWorker` (mic → Flux → GrammarGate → speaker), torn down at session end | Pinned-source recon: Flux connects on `StartFrame` with no app-facing connect/disconnect, and its watchdog injects **billed** silence into stalled turns — per-session workers give fresh sockets and $0 idle by construction | BT (session loop unit) + HW: wake→command feel, back-to-back sessions | |
| 8 | wake | oWW models live in the venv's package dir (`download_models` default target), auto-fetched on first run per machine; `voice/models/` is unused | `Model()` resolves feature models from package resources; a custom dir would strand them | BT (test_wake downloads + detects) | |
| 9 | session | HOLDING/LINGER = `PipelineWorker.idle_timeout_secs` = `holdWindowS`, with speech/transcript/bot frames resetting the clock; exit phrases push `EndWorkerFrame` immediately | One timeout mechanism instead of gate-managed timers; Pipecat owns the clock | HW: window feel — tune `holdWindowS` | |
| 10 | wake→speech gap | User's first words after the wake tick may race Flux's ~200–400 ms connect; the tick invites a natural pause and no mitigation is built | Buffering pre-connect audio adds complexity for an unproven problem | HW: if first words clip, add a pre-roll buffer | |
| 11 | stt | `mip_opt_out=True` always (privacy over the ~2× metered rate) | Design-doc stance | Deepgram console shows the flag | |

| 12 | titles | `{game}` is a hassil **wildcard**; all title matching lives in the fuzzy resolver (exact-variant short-circuit → fuzzy with ambiguity refusal: near-ties across different games return "no match" — saying no beats launching wrong). Names are ASCII-sanitized at both sources | Real-library tests: no colon in "ARMORED CORE VI FIRES OF RUBICON" broke list matching; token_set scores subsets at 100 (Warhammer 40K tie); PS5.1 mojibake'd ™ | BT (38/38 round-trips, 10/10 spoken phrasings) + HW: fuzzy torture drill | |
| 13 | launch | "play Y" while X runs ⇒ truthful refusal (`BUSY:<id>`), same appid ⇒ `ALREADY`, mid-launch ⇒ "still starting"; owned-but-not-installed ⇒ spoken decline (install flow is C4) | Design-doc no-force-switch policy | HW: launch drills | |
| 14 | REPL | `--text` REPL is **always dry-run** (actions log, never execute) | Bench instrument; typing "end session" at a keyboard mid-game shouldn't kill the game | Your first REPL session | |
| 15 | tts | Aura-2 default voice `aura-2-thalia-en` (unauditioned); Kokoro behind `ttsLocal: true` requires `pip install "pipecat-ai[kokoro]"` and its ctor **blocks on a ~300 MB first-run download** | Audition needs ears; Kokoro kept optional to keep deploys light | HW: voice audition, then set `ttsVoice` | |
| 16 | catalog | Pre-metadata catalog rows are sparse (`\|\|\|0h\|never`) until `library.py refresh --owned --meta` runs with keys; appid 228980 (redistributables) excluded | Layers 2–3 need Steam key + a crawl session | KEY + one `--meta` run (~80 s for 38 games) | |
| 17 | assistant | Cross-session carry = last 8 context messages in process memory (lost on agent restart); in-session history native | Simplest thing that makes "play the second one" work | HW: follow-up drill | |

(rows appended as the build proceeds)

## Open questions

(appended as they arise; each gets an owner: a drill, a key, or a decision of yours)
