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
| 9 | session | Session-end = `PipelineWorker` idle timeout (`holdWindowS`), but `cancel_on_idle_timeout=False` + an `on_idle_timeout` handler that defers while `GrammarGate.is_busy()` (user mid-turn OR a dispatch in flight) — so a long utterance or a 15 s ssh call can't cut the session mid-word. Exit phrases push `EndWorkerFrame` immediately | Flux emits no frame mid-turn and blocking dispatch pushes nothing, so a raw idle timeout would fire mid-utterance/mid-ssh (review bug) | HW: window feel — tune `holdWindowS` | |
| 10 | wake→speech gap | User's first words after the wake tick may race Flux's ~200–400 ms connect; the tick invites a natural pause and no mitigation is built | Buffering pre-connect audio adds complexity for an unproven problem | HW: if first words clip, add a pre-roll buffer | |
| 11 | stt | `mip_opt_out=True` always (privacy over the ~2× metered rate) | Design-doc stance | Deepgram console shows the flag | |

| 12 | titles | `{game}` is a hassil **wildcard**; all title matching lives in the fuzzy resolver (exact-variant short-circuit → fuzzy with ambiguity refusal: near-ties across different games return "no match" — saying no beats launching wrong). Names are ASCII-sanitized at both sources | Real-library tests: no colon in "ARMORED CORE VI FIRES OF RUBICON" broke list matching; token_set scores subsets at 100 (Warhammer 40K tie); PS5.1 mojibake'd ™ | BT (38/38 round-trips, 10/10 spoken phrasings) + HW: fuzzy torture drill | |
| 13 | launch | "play Y" while X runs ⇒ truthful refusal (`BUSY:<id>`), same appid ⇒ `ALREADY`, mid-launch ⇒ "still starting"; owned-but-not-installed ⇒ spoken decline (install flow is C4) | Design-doc no-force-switch policy | HW: launch drills | |
| 14 | REPL | `--text` REPL is **always dry-run** (actions log, never execute) | Bench instrument; typing "end session" at a keyboard mid-game shouldn't kill the game | Your first REPL session | |
| 15 | tts | Aura-2 default voice `aura-2-thalia-en` (unauditioned); Kokoro behind `ttsLocal: true` requires `pip install "pipecat-ai[kokoro]"` and its ctor **blocks on a ~300 MB first-run download** | Audition needs ears; Kokoro kept optional to keep deploys light | HW: voice audition, then set `ttsVoice` | |
| 16 | catalog | **All three layers now auto-sync** via `library.sync()` on the agent's background thread (startup + after each session): installed when the PC is awake, owned when stale >6h, metadata top-up for new appids — Steam layers run even while the PC sleeps (cloud, no PC needed), key-gated, non-reentrant. So the catalog fills itself once the Steam key is in `secrets.json`; no manual CLI needed. First fill crawls ~2 s/game in the background. appid 228980 (redistributables) excluded | The Web API layers need no PC and are the higher-value/lower-cost half — leaving them manual was a build gap (user-flagged), and the cadence was inverted (auto-refreshed the PC-dependent layer, left manual the cloud one) | KEY in secrets.json, then watch the catalog populate over the first session or two | |
| 17 | assistant | Cross-session carry = last 8 context messages in process memory (lost on agent restart), **run through `_trim_carry`** so a slice never starts on an orphaned tool_result (Anthropic 400) or ends on an unpaired tool_call; in-session history native | Simplest thing that makes "play the second one" work, made 400-safe (review bug) | HW: follow-up drill | |

## Review fixes (multi-agent pass, 2026-08-10)

11 confirmed bugs + selected quality/slop findings applied on-branch, all with blind coverage. The load-bearing ones:

- **Blocking dispatch off the event loop** — GrammarGate and the assistant tool handlers now `asyncio.to_thread` every ssh/serial call; a 15 s host-asleep "end session" no longer freezes audio, the Flux socket, and TTS.
- **Idle-timeout guard** (row 9) — sessions no longer cancel mid-utterance or mid-dispatch.
- **`_trim_carry`** (row 17) — follow-up sessions no longer 400 on split tool pairs.
- **`resolve('it')`** — single short stopword/pronoun queries refuse instead of launching a subset-matched title ("play it" → assistant, not It Takes Two).
- **`refresh_owned` under system python** — `load_secrets`/`real_key` moved to `cglib`; `library.py refresh --owned` no longer drags pipecat/hassil in and no longer crashes off-venv.
- **Empty `launch-app` marker** — stringify-before-trim; the consume-once file protocol survives a zero-byte marker (PS 5.1 `Get-Content`→`$null`).
- **`--devices` crash** — spike's argv parse moved under `__main__`; the first hardware drill works.
- **`secrets.json` BOM/typo** — `utf-8-sig` + `ValueError` caught; malformed secrets disable lanes, never crash-loop the supervisor.
- **Library refresh hooks** — now wired (background thread at startup + after each session), so "play &lt;title&gt;" works cold.
- **COM-port retry** moved into `cglib.exlink_send_hex` so couch.py's production power/input sends get the same contention protection.
- **Config validation** — missing `voice` keys fail loudly at startup, not per-wake; `.get` defaults removed so config.json is the one home.
- Quality/slop: `_vol_steps` helper (twin dedup), single library.json reader, `GrammarMatcher` built once, dead `titles` param + dead config keys + unreachable returns removed, `paInt16` constant, header-comment de-slop, Start-Voice bootstrap gated on a `deps-ok` sentinel.

Still open (recorded, not yet applied — deliberately deferred to the hardware pass): Ex-Link **ack validation** in `_exlink` (needs the C1 `probe_volume` result to know the real ack bytes before rejecting on non-`030cf1`).

## Open questions

(appended as they arise; each gets an owner: a drill, a key, or a decision of yours)
