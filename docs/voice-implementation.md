# Voice Control — implementation plan

Architecture: [voice-control-design.md](voice-control-design.md). This is the
full build plan — every phase at commit granularity. Because Pipecat releases
weekly and the spike already caught one API deprecation, each phase opens with
the **phase-open ritual** instead of trusting this document blindly:

1. Confirm the pinned deps still install clean on both machines.
2. Re-read the *pinned version's* docs for every Pipecat service this phase
   touches; copy signatures from there, never from tutorials.
3. If anything drifted, amend this plan first, then build.

Division of labor, as ever: Claude ships code + exact drills; Tillman runs them
on real hardware and pastes results. Every commit ends in a paste-back. Completed
work collapses to **as-built** records (kept forever); plans for done work are
deleted.

## Status ledger

| Phase | State | Gate result |
|---|---|---|
| **C1 — command lane** | **current** — commit 1 partially done (spike ✅) | step-0 spike: **PASS** (gaming PC, 2026-08-10; as-built below) |
| C0 — acoustic gate | protocol ready; **blocked on array delivery**; consumes C1 commits 1–2 | — |
| C2 — game launch | planned below; opens when C1's gate closes | — |
| C3 — conversation lane | planned below; opens when C2's gate closes | — |

## Cross-cutting decisions (all phases)

- **Runner API**: `WorkerRunner` + `add_workers()` from day one — `PipelineRunner`
  is deprecated since 1.3.0, removed in 2.0 (spike finding).
- **One dispatch module**: `voice/dispatch.py` owns every side effect (spawn
  `couch.py start [appid]`, `ssh gamepc exit|launch|games|playing`, cglib
  Ex-Link calls, earcons). GrammarGate (C1) and the LLM tool handlers (C3) call
  the same functions — two lanes, one set of hands.
- **Failure-domain rule**: `k15/voice/` is a folder unit with its own `.venv`;
  the chord listener stays on system-PATH python, untouched, forever.
- **Logging**: everything tags `[voice]` into `couch.log` via `cglib.make_log`.
- **Dependency policy**: `requirements.txt` grows per commit, always `==` pins;
  upgrades are deliberate events regression-tested with the current phase's
  drill suite.
- **Secrets timeline**: `deepgramApiKey` needed at C1 commit 3 · `steamApiKey` +
  `steamId64` at C3 commit 1 · `anthropicApiKey` at C3 commit 2.
- **Deploy model**: K15 = copy `k15/voice/` + any touched sibling files to the
  Desktop, listener console closed, venv is machine-local (never copied). PC
  (C2 only) = copy touched files to `C:\CouchGaming\`, no session active.
  **First K15 deploy reruns `spike.py` there** — that's the deferred
  K15-confirmation, then the machine is trusted.

### `config.json` additions (final schema, introduced incrementally)

```json
"voice": {
  "inputDeviceName": "",            // substring match on device name; "" = default
  "outputDeviceName": "",
  "wakeModel": "hey_jarvis",        // custom "hey console" swaps in later
  "wakeThreshold": 0.5,
  "eotThreshold": 0.7,              // Flux end-of-turn confidence
  "eagerEotThreshold": 0.5,         // speculative start; 0 disables
  "lingerS": 5,                     // post-command chained-command window
  "holdWindowS": 8,                 // post-answer follow-up window
  "followupCarryS": 60,             // cross-session context carry
  "bargeInMinWords": 2,
  "fuzzyTitleThreshold": 87,        // rapidfuzz token_set_ratio floor
  "keytermCount": 40,               // Flux keyterm budget (500-token cap upstream)
  "volumeStep": 5,
  "volumeMax": 40,                  // voice can never blast the room
  "ttsVoice": "aura-2-thalia-en",   // C3 audition decides
  "assistantModel": "claude-haiku-4-5"
}
```

---

## C1 — command lane (current)

**Exit gate:** every command class works by voice end-to-end — session
start/end, volume/mute, input switching — at ≤0.5 s end-of-speech → earcon;
chord listener provably unaffected; supervisor restart survives; K15 spike
rerun passed; pins recorded.

### Commit 1 — `voice: spike kit` ✅ shipped (`1e04f4a`, `5468cf6`)

**As-built (2026-08-10): PASS — run on the gaming PC** (K15 confirmation rides
the first K15 deploy). Python 3.11.4, `pipecat-ai==1.7.0`, `pyaudio==0.2.14`
(cp313 win_amd64 wheel confirmed for the K15's 3.13). Capture steady at 49.6
frames/s (= the expected 50 × 20 ms chunks); 4 speech events → 4 tones played
while capture continued (duplex, the barge-in prerequisite, works); zero device
errors. 11 s run by design — the 10-minute soak moves to C0 prep on the real
array. Findings: `PipelineRunner` deprecated → build on `WorkerRunner`;
sounddevice escape hatch not needed.

### Commit 2 — `voice: skeleton + wake gate + bench modes`
`voice_agent.py` (WorkerRunner skeleton, config/secrets loading, `[voice]`
logging), `earcons.py` (synthesized at startup; count vocabulary mirrors the
haptic thuds: 1 ok / 2 busy / 3 fail + wake tick), `wake_gate.py`
(OpenWakeWordGate FrameProcessor: oWW ONNX `hey jarvis`, model auto-fetch to
`voice/models/`, gate closed = zero frames downstream, zero cloud). Bench
modes: `--wake-trials` (per-detection log with confidence — C0's instrument)
and `--false-accept-soak` (spurious-wake counter). *Deps added:*
`openwakeword` + pinned onnxruntime. *Drills:* 10 wakes at ~10 ft in a quiet
room (code check, not the acoustic gate); 1-hour TV soak; CPU% at idle
(expect ~nil).

### Commit 3 — `voice: flux session + grammar gate` *(needs `deepgramApiKey`)*
Flux socket lifecycle (open on wake, close at DORMANT — no idle socket exists
on Flux v2), `grammar_gate.py` + `grammar.yaml` (hassil templates: start/end
session — end is exact-match-only; volume up/down/set/mute; input switching;
exit phrases), earcon acks, LINGER window, lock-arbiter busy check (mirrors
the chord's 2-thud rule). Dispatch calls stubbed to log-only. *Deps:*
`deepgram-sdk` (or Pipecat's deepgram extra — ritual decides), `hassil`,
`rapidfuzz`. *Drills:* offline transcript table via `voice/test_grammar.py`
(runs anywhere, no audio); live: each template phrased 3 ways → correct earcon
+ `[voice]` log line; end-of-speech → earcon stopwatch (target ≤0.5 s);
deliberate garble → no match, no action.

### Commit 4 — `k15: exlink volume/mute frames`
cglib: checksum builder + frozen frames (vol_up `082201000100d4`, vol_down
`082201000200d3`, mute_toggle `082202000000d4`, parametric vol_set with
`volumeMax` clamp). exlink.py CLI inherits the names automatically. *Drills
(TV on, from the K15):* each frame once → `030cf1` ack + visible TV response;
the one-shot volume-query probe (500 ms read) — answer decides real mute state
vs software-tracked; mute desync recovery via absolute vol_set.

### Commit 5 — `voice: dispatch wiring`
`dispatch.py` created; GrammarGate stubs go live: `couch.py start` spawn
(listener's pattern), `ssh gamepc exit`, cglib exlink calls. *Drills:* voice
session start from cold — TV untouched until READY (the one rule, observed);
voice "end session" mid-game (the exit asymmetry, closed); volume/mute/input
in every system state incl. mid-session; chained commands inside LINGER; busy
buzz on double-start.

### Commit 6 — `voice: supervisor + doctor + docs`
`Start-Voice.bat` (venv activate, `xvf_host REBOOT 1` gated on presence —
no-ops until the array exists, restart-with-backoff loop, close-window-to-stop),
doctor.py voice rows (venv + pins, mic device, wake model, Deepgram auth,
grammar parses, index age placeholder), README rows, as-built here. *Drills:*
kill voice python → restart ≤10 s; kill mid-session → chord/listener untouched;
doctor healthy pass + induced FAIL (rename secrets.json); **first full K15
deploy incl. spike rerun on the K15**.

---

## C0 — acoustic acceptance gate (when the array arrives)

Unchanged protocol; consumes commit 2's bench modes.

1. **Prep:** USB3/xHCI port; Zadig WinUSB on the control interface;
   `xvf_host VERSION` smoke; UA firmware confirmed (2-ch 16 kHz); **10-minute
   spike soak on the array** (the deferred one); speaker-out audibility over
   TV audio from the couch.
2. **Aim (placement 1: atop console):** `AEC_FIXEDBEAMSONOFF 1` → both
   azimuths/elevations at couch seats (measured, slight up-tilt) →
   `AEC_FIXEDBEAMSGATING 1` → validate via `AEC_SPENERGY_VALUES` + LED DoA →
   only after live proof: `SAVE_CONFIGURATION 1` **once** (Safe-Mode recovery
   chord — hold mute at boot — known *before* saving).
3. **Trials:** `--wake-trials` 20× per condition {movie volume, loud movie} ×
   {couch-left, couch-right}; `--false-accept-soak` through one ~2 h movie.
4. **Gate:** ≥18/20 every condition, ≤1 false accept/movie → record placement +
   azimuths, done. Miss → placement 2 (in-cabinet, foam pad) → repeat → beam-energy
   double-gate in wake_gate → repeat → stop and reassess (design-doc top risk).
5. Results table lands here as-built regardless — C3's barge-in tuning wants it.

---

## C2 — game launch

**Exit gate:** "play <title>" works from cold (full chain into the game) and
mid-session; wrong-state cases refuse politely (BUSY/NOTREADY); fuzzy matching
survives the torture list; installed-index refresh works without a thought.

### Commit 1 — `gaming-pc: launch/games/playing verbs + LaunchGame task`
Dispatch.ps1 grows to six verbs, posture unchanged (allowlist, built-ins only,
default DENIED): `games` (SteamPath from registry → `libraryfolders.vdf` →
every `appmanifest_*.acf` → regex appid/name/SizeOnDisk/LastPlayed/StateFlags →
`ConvertTo-Json -Compress`, ~25 lines, read-only); `playing` (RunningAppID,
0 = none); `launch <appid>` (regex `^launch \d{1,10}$` → ready marker else
`NOTREADY` → RunningAppID: same appid `ALREADY`, different `BUSY:<id>` → write
appid to `C:\ProgramData\CouchGaming\launch-app` → `schtasks /Run
\CouchGaming\LaunchGame` → `OK`/`FAILED:<code>`). New `Launch-Game.ps1`
(transcript, read + delete marker, re-validate numeric, SteamPath registry →
`steam.exe -applaunch <appid>`); task registered with the Enter/Exit idiom
(5-min limit, no elevation). Doctor.ps1: LaunchGame task row + stale-marker
check. *Drills (from K15 ssh):* `games` → valid JSON, count matches reality;
`playing` idle → 0; desk-side `launch` with no session → `NOTREADY`; in-session
`launch <installed>` → game boots into Big Picture; `launch <other>` while
running → `BUSY:<id>`, nothing launches; same appid → `ALREADY`.

### Commit 2 — `voice: library.py layer 1 + keyterm feed`
`library.py`: `refresh` (ssh `games` → parse → atomic merge into
`state/library.json`), `show`, keyterm derivation (top `keytermCount` installed
titles by LastPlayed → consumed at Flux session open). voice_agent refresh
schedule: at startup if port 22 answers; after each "end session" dispatch
(PC provably awake); every 6 h opportunistically; manual CLI always. Never a
blind nightly. *Drills:* refresh with PC awake → JSON sane; refresh with PC
asleep → clean skip, no error spam; keyterm list eyeball (recent titles
present, ≤40).

### Commit 3 — `voice: play {game} + couch.py appid`
grammar.yaml gains `(play|launch|start|put on) {game}`; GrammarGate fuzzy-binds
{game} over installed titles (`fuzzyTitleThreshold`, below → no-match earcon —
until C3 gives it to the assistant). `couch.py start [appid]`: after the input
switch, best-effort `ssh launch <appid>` (a failed game launch never fails the
session — Big Picture up is a working outcome). dispatch.py routes: lock
fresh + READY → direct ssh launch (BUSY → busy earcon); no session → spawn
`couch.py start <appid>` (1-tone earcon). *Drills:* "play <title>" from cold —
one chord-free chain into the game; mid-session switchless launch; "play Y"
during X → refusal earcon + log; fuzzy torture ("armored core", "AC6",
"forza" for Forza Horizon 5, a title that shouldn't match anything); STT
accuracy spot-check on the 5 hardest installed titles (keyterms earning keep).

**C2 deploy:** PC: Dispatch.ps1, Launch-Game.ps1, Doctor.ps1 + one elevated
task registration (guide-style ELI5 provided). K15: voice/ + couch.py.
**Rollback:** verbs are additive; task deregisters with one schtasks command;
couch.py appid param is inert when absent.

---

## C3 — conversation lane

**Exit gate:** the design doc's drills — multi-turn mech-games → "play it"
launches a validated appid; barge-in mid-answer; "volume up" mid-conversation
provably skips the LLM; TV-noise movie soak with ≤1 false turn; offline drill
(no internet = voice down by design, chord unaffected; Aura-only outage =
Kokoro speaks). Latency: ≤1.5 s end-of-speech → first audio (measure both
eager on/off).

### Commit 1 — `voice: library layers 2+3 + catalog` *(needs Steam key + steamid64)*
`library.py` grows: `refresh --owned` (GetOwnedGames: playtime_forever/2wks,
rtime_last_played — canonical recency), `refresh --meta` (appdetails genres/
categories 28|18/desc/metacritic + SteamSpy tags-with-votes; ≤1 req/2 s;
`state/metadata-cache.json` cached forever, top-up new appids only), `catalog`
(emits the compact prompt rows: `appid|title|top5 tags|hours|last_played|
installed|controller`). Owned refresh daily; meta top-up after installed
refreshes. *Drills:* cold crawl of the full library (rate-limit respected —
watch for 429s); tag spot-check 5 games vs the store; `catalog` token count
(expect ~6–18K; ceiling check vs cache minimum 4,096 — it clears).

### Commit 2 — `voice: assistant brain` *(needs `anthropicApiKey`)*
`assistant.py`: AnthropicLLMService (`assistantModel`, `Settings` pattern —
not the deprecated kwargs), system prompt (voice-answer style rules: lead with
the count, name ≤3, never recite >4 titles, ≤2 sentences unless asked),
catalog block + `cache_control` breakpoint, strict tools `launch_game` /
`get_game_details` / `get_now_playing` / `control` — handlers call dispatch.py
(same hands as GrammarGate), `launch_game` re-validated against the index.
GrammarGate no-match now flows to the context aggregator; in-session
multi-turn native; `followupCarryS` cross-session stash. **`--text` REPL
mode**: type transcripts, see replies + tool calls — prompt/tool iteration
without audio. *Drills (REPL first):* 20-query canned set (Q&A, recommend,
launch-by-description, "the second one" follow-up, garbage in); tool-call
validation (invented appid must be impossible); then the same set live-voice.

### Commit 3 — `voice: speech out + conversation UX`
Voice audition script (one sentence in every `aura-2-*-en` voice → user picks
→ `ttsVoice`); Aura-2 WebSocket service with sentence-chunked streaming;
HOLDING window; barge-in enabled (`bargeInMinWords`, sustained-speech ~250 ms,
energy floor) — tuned against real TV audio; KokoroTTSService auto-fallback on
Aura failure; exit phrases close with a soft earcon. *Drills:* the C3 gate
list above, plus: latency stopwatch ×10 queries (eager on/off compared);
barge-in 10× mid-answer (flush <150 ms by ear); movie-soak false-turn count;
pull WAN mid-session → commands dead with fail earcon, chord alive (overlay
rule observed); Aura-blocked-only test → Kokoro speaks.

### Commit 4 — `voice: A/B, polish, docs`
Model A/B (Haiku vs current fast-tier alternatives incl. whatever Luna/mini
looks like then): same 20-query set, measured end-of-speech → first-word +
blind answer-quality judgment — data decides `assistantModel`. README final
rows; design-doc deltas if reality disagreed; as-builts collapse here.

**C3 deploy:** K15 only (voice/ + library state bootstraps on-machine).
**Rollback:** conversation lane is the GrammarGate fallthrough — config flag
turns it off and C2 behavior returns; Aura/Kokoro/model are config strings.

---

## What could still change this plan

| Trigger | Effect |
|---|---|
| C0 gate fails at both placements + double-gate | Project pauses at C1 scope (voice works desk-quality: near-field mic) — the design doc's top risk, decided by data |
| Phase-open ritual finds Pipecat drift | Plan amended first (this file), signatures from pinned docs, then build |
| C3 latency misses ≤1.5 s measured | gpt-realtime-2.1 drop-in for the conversation lane (design doc §Rejected has the exact pattern) |
| Flux misbehaves (turn quality, socket flake) | Nova-3 + smart-turn v3 swap — one service line each, both already scoped |
