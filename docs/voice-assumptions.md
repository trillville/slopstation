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

(rows appended as the build proceeds)

## Open questions

(appended as they arise; each gets an owner: a drill, a key, or a decision of yours)
