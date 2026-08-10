# Voice Control — full implementation plan (living document)

Architecture: [voice-control-design.md](voice-control-design.md). This file is
the build: every phase at commit granularity. It stays living — each commit's
drill results append to that phase's **as-built**; deviations get recorded, not
hidden. One stated caveat: Pipecat moves weekly, so C3's exact service
constructor signatures are re-verified against the **pinned** version's docs at
build time — the spike already caught `PipelineRunner` deprecated in favor of
`WorkerRunner`, which is now the standing example of why.

## Ledger

| Phase | State | Gate |
|---|---|---|
| **C1 — command lane** | **current**; step 0 spike **PASS** (gaming PC, 2026-08-10) | every command class by voice, ≤0.5 s, chord untouched, K15-confirmed |
| C0 — acoustic gate | blocked on array delivery; consumes C1 step 2 bench modes | ≥18/20 wakes per condition, ≤1 false accept per movie |
| C2 — game launch | planned below; starts when C1 closes | "play \<installed title\>" works in any system state |
| C3 — conversation lane | planned below; starts when C2 closes | design-doc example flows pass live at target latency |

**Conventions (all phases):** Claude ships code + numbered drills; Tillman runs
them on hardware and pastes results — no commit is "done" until its drill
passes. Every touched `.py` gets `py_compile`, every `.ps1` gets
`Parser::ParseFile`, before commit. Deploy = copy listed files to the machine
(never clone); the voice venv is created on-machine, never copied. Dependency
pins are frozen in `requirements.txt` at each commit and recorded in the
as-built. Rollback for every K15 voice commit: close the Start-Voice window —
the system is byte-for-byte pre-voice; PC-side rollback is per-file
`git checkout` + re-copy (C2 lists its extra step).

---

## C1 — command lane (current)

**Exit criteria:** start/end session, volume/mute, input switching by voice at
≤0.5 s end-of-speech → earcon; supervisor survives kill; chord listener
provably unaffected; drills re-run green on the K15 (step 6).

### Step 0 — spike ✅ as-built (2026-08-10)

**PASS on the gaming PC** (K15 confirmation folded into step 6's deploy).
Python 3.11.4, `pipecat-ai==1.7.0`, `pyaudio==0.2.14` (cp313 win_amd64 wheel
confirmed for the K15's 3.13). Capture steady at 49.6 frames/s (= expected
50 × 20 ms), 4/4 speech→tone with capture continuing (duplex proven), zero
errors. Soak deferred to C0 prep on the real array. Findings: build on
`WorkerRunner.add_workers()` (`PipelineRunner` dies in 2.0); sounddevice
escape hatch not needed.

### Step 1 — `voice: scaffold + supervisor`
- `voice_agent.py` skeleton: parent-path bootstrap (`sys.path` insert → import
  `cglib`), load `../config.json` + `../secrets.json`, `cglib.make_log("voice")`
  (one `couch.log`, `[voice]` tag), device resolution by config name-fragment →
  `input_device_index`/`output_device_index`, `WorkerRunner` shell, `--devices`
  flag (prints the spike's device table and exits).
- `earcons.py`: synthesized tones, count vocabulary mirroring the thuds —
  1 = accepted, 2 = busy, 3 = failed, plus wake-tick and session-close ticks.
- `Start-Voice.bat`: venv bootstrap (create + `pip install -r` if missing),
  supervisor restart loop with 10 s backoff (same shape as Start-Listener),
  `rem` close-window-to-stop; array `xvf_host REBOOT 1` slot present but gated
  on the exe existing (no-ops until the array is real).
- README: voice rows in the K15 table.

**Drill 1:** bat cold-starts on the gaming PC; `[voice]` startup line lands in
couch.log; Task Manager kill → auto-restart ≤10 s; close window → stays dead.

### Step 2 — `voice: wake gate + bench modes`
- `wake_gate.py` (FrameProcessor): buffers 20 ms input frames to oWW's 80 ms
  hop, runs `hey_jarvis` (ONNX; auto-download to `voice/models/` on first run),
  config `wakeThreshold`. Gate CLOSED = input audio swallowed (nothing flows
  downstream — no cloud, by construction). On detection: wake-tick earcon, gate
  OPEN, timers armed (`lingerS`, `holdWindowS` consumed in step 3).
- Bench modes on `voice_agent.py`: `--wake-trials` (log each detection +
  confidence + running tally) and `--false-accept-soak` (spurious-wake count +
  rate/hr). These are C0's instruments.
- Deps: `+openwakeword==0.6.0` (onnxruntime already core via pipecat, `~=1.24.3`).

**Drill 2:** 10 deliberate wakes, quiet room, any mic (code check, not the
acoustic gate) — expect ≥9 detections; 1-hour TV soak → false-accept count
(number recorded, no gate — that's C0's job on the real array).

### Step 3 — `voice: flux session + grammar gate` *(the one remaining technical unknown lives here)*
- **Session lifecycle first**: verify against pinned source how
  `DeepgramFluxSTTService` behaves when audio stops ~30 s (socket death per
  SDK issue #649). Candidate A: wake gate simply starts/stops the audio flow
  and the service reconnects on demand (if its `_connect` is lazy/re-entrant).
  Candidate B: wake gate drives explicit service `start()`/`stop()` via events.
  Whichever the source supports cleanly wins; decision recorded in as-built.
  Billing invariant either way: **socket open ≈ session open only**.
- `grammar_gate.py` (FrameProcessor): hassil match over `grammar.yaml` —
  `session_start`, `session_end` (**exact match only**), `vol_up`, `vol_down`,
  `vol_set{level}`, `mute`, `input_switch{name}`, exit phrases ("thanks",
  "that's all", "never mind"). Match → swallow frame, ack earcon, dispatch
  (this commit: log-only stubs `[voice] would: ...`). No match → pass through
  (dead-ends until C3; fail earcon + log in the interim). LINGER (~5 s
  post-command) and HOLDING windows enforced via the wake gate's timers.
- `test_grammar.py`: offline utterance→intent table, runnable with no audio.
- Deps: `+pipecat-ai[deepgram]`, `+hassil` (pinned at commit).

**Drill 3:** offline grammar table green; live: each command 3 phrasings →
correct `[voice] would:` lines; "volume up" twice inside LINGER without
re-waking; Deepgram console usage ≈ session seconds (billing sanity);
30 s silence → session closes with tick.

### Step 4 — `k15: exlink volume/mute frames`
- `cglib.py`: checksum builder + frozen frames — `vol_up 082201000100d4`,
  `vol_down 082201000200d3`, `mute_toggle 082202000000d4`, parametric
  `vol_set(0-100)`. Computed, never hand-typed (a one-byte slip in this family
  is power_off). `exlink.py` CLI picks the names up automatically.

**Drill 4 (TV on, from K15):** each frame once → `030cf1` ack + visible TV
response; the 500 ms volume-query probe (`0822f0010000e5`) — reply ⇒ real mute
state forever, silence ⇒ software-tracked mute + absolute-set resync (result
recorded in as-built either way). Never send key-codes 0x3B/0x3C.

### Step 5 — `voice: dispatch wiring`
- Stubs → real, all through one dispatch module shared later with C3's tools:
  `session_start` → `cglib.lock_age()` fresh ⇒ busy earcon, else spawn
  `couch.py start` (new console, exactly the listener's pattern);
  `session_end` → `ssh gamepc exit` (15 s timeout; OK ⇒ ack, raise ⇒ fail
  earcon); volume/mute/input → `cglib.exlink_send` (config COM port,
  `volumeMax` clamp on `vol_set`, one retry on transient serial-open failure —
  couch.py and voice share COM3 in open-write-close bursts).

**Drill 5 (the C1 payoff):** cold voice "start a session" → full chain, TV
untouched until READY (the one rule, witnessed); "end session" **mid-game** —
the exit asymmetry closed; busy path (chord launch then voice start ⇒ 2-tone);
volume/mute/input in idle + mid-session; chord still works with voice running.

### Step 6 — `voice: doctor + K15 confirmation + as-built`
- `doctor.py` voice rows: venv exists + pins match requirements, input device
  resolves by name, wake model file present, Deepgram key auths (1 s probe),
  `grammar.yaml` parses, config voice keys present. Same PASS/WARN/FAIL format.
- Start-Voice finalized; README/docs;
- **First K15 deploy**: copy `k15/voice/` + updated `cglib.py`, `config.json`,
  `doctor.py`, `exlink.py` → Desktop; venv bootstrap; **re-run spike.py on the
  K15** (its 3.13 + drivers — the deferred confirmation), then drills 1/3/5
  condensed. C1 gate closes on this paste-back; pins frozen in as-built.

---

## C0 — acoustic gate (execute on array arrival; needs C1 ≥ step 2)

1. **Prep:** USB3/xHCI port; Zadig WinUSB on the control interface;
   `xvf_host VERSION`; confirm UA/USB 2-ch 16 kHz firmware; speaker-out
   audibility at couch distance; **the deferred 10-minute spike soak, on the
   array**; enable the REBOOT line in Start-Voice.bat.
2. **Aim (candidate 1: atop console):** `AEC_FIXEDBEAMSONOFF 1` → both
   azimuths/elevations at couch-left/right (slight up-tilt) →
   `AEC_FIXEDBEAMSGATING 1` → validate speaking from each seat via
   `AEC_SPENERGY_VALUES` + LED DoA → only after live config proves out:
   `SAVE_CONFIGURATION 1` **once** (Safe-Mode recovery = hold mute at boot;
   learn it before saving — the brick bug is real).
3. **Trials:** `--wake-trials` 20× per condition {movie volume, loud movie} ×
   {couch-left, couch-right}; `--false-accept-soak` through one ~2 h movie.
4. **Gate:** ≥18/20 every condition AND ≤1 false accept/movie ⇒ pass; record
   placement + azimuths + the full table here. Miss ⇒ candidate 2 (in-cabinet,
   foam, against grate) ⇒ still miss ⇒ enable beam-energy double-gate in
   wake_gate ⇒ still miss ⇒ stop, reassess mount geometry (design-doc top risk).

---

## C2 — game launch (starts when C1 closes)

**Exit criteria:** "play \<installed title\>" from the couch works cold (full
launch into the game), mid-session (direct launch), and refuses truthfully
(busy earcon) when another game is running. No new secrets needed — the
installed list rides SSH; Steam Web API waits for C3.

### Step 1 — `pc: games/launch/playing verbs + LaunchGame task`
- `Dispatch.ps1` 3→6 verbs, posture unchanged (allowlist, built-ins only):
  - `games`: parse `libraryfolders.vdf` paths → regex each `appmanifest_*.acf`
    for appid/name/SizeOnDisk/LastPlayed/StateFlags → compact JSON to stdout.
  - `launch <appid>`: `^\d{1,10}$` or DENIED → ready marker absent ⇒
    `NOTREADY` → `RunningAppID` ≠ 0 and ≠ target ⇒ `BUSY:<id>` → write appid
    to `C:\ProgramData\CouchGaming\launch-app` → `schtasks /Run \CouchGaming\LaunchGame`
    → `OK`/`FAILED:<code>`. (Pre-checks in Dispatch so failures return
    synchronously; file-as-argument because the task needs the interactive
    session.)
  - `playing`: emit `HKCU\...\Steam\RunningAppID` (0 = none).
- `Launch-Game.ps1` + registration (same idiom as Enter/Exit incl. the
  load-bearing 5-min execution limit, non-elevated): read+delete marker,
  re-validate numeric, SteamPath from registry, `& steam.exe -applaunch <id>`.
- `Doctor.ps1`: LaunchGame task registered; stale `launch-app` marker warning.
- Registration commands recorded in this file (the guide stays frozen).

**Drills:** from K15 — `ssh gamepc games` JSON count matches steamapps;
`playing` = 0 idle; `launch` outside session ⇒ `NOTREADY`; in session: real
appid ⇒ game boots into Big Picture; second launch while running ⇒ `BUSY:<id>`;
`bogus` still `DENIED`. Rollback: checkout Dispatch/Doctor, re-copy,
`schtasks /Delete /TN \CouchGaming\LaunchGame`.

### Step 2 — `k15: library layer 1 + keyterms`
- `library.py`: `refresh` (ssh `games` → parse → atomic `state/library.json`),
  `show`; keyterm list = top-`keytermCount` installed by LastPlayed.
- Hooks in voice_agent: refresh at startup if PC reachable, after each
  `session_end` dispatch, and manual CLI. Never scheduled against a sleeping PC.

**Drills:** refresh with PC awake → count matches; PC asleep → one clean
`[voice] library refresh skipped` line; keyterms file inspected.

### Step 3 — `voice: play {game}`
- `grammar.yaml` gains `play|launch|start|put on {game}`; `{game}` resolved
  against library titles — hassil list slot + rapidfuzz `token_set_ratio ≥ 87`
  (below ⇒ fail earcon in C2; becomes the C3 fallthrough later). Deps:
  `+rapidfuzz`.
- Dispatch: lock fresh + READY ⇒ `ssh launch <appid>` (`BUSY:` ⇒ busy earcon);
  no session ⇒ `couch.py start <appid>`.
- `couch.py`: optional argv appid — after the input switch, best-effort
  `ssh launch <appid>` (a failed game launch never fails the session; Big
  Picture is already a working outcome). Chord path passes nothing —
  byte-for-byte today's behavior.
- Flux keyterms wired from the library.

**Drills:** cold "play armored core six" → thud-free full chain into the game;
mid-session direct launch ≤3 s; "play Y" during X ⇒ busy earcon + `BUSY:` log;
fuzzy torture ("armored core", "the new armored core", partials); a
non-installed title ⇒ fail earcon (C3 will turn this into speech).

---

## C3 — conversation lane (starts when C2 closes)

**Exit criteria:** the design doc's example flows live — multi-turn mech-games
Q&A → "play it" launches with a validated appid; barge-in mid-answer; "volume
up" mid-conversation with zero LLM involvement in the log; measured
end-of-speech → first-audio ≤1.5 s; a week's spend within budget.
**New secrets:** `anthropicApiKey`, `steamApiKey` + `steamId64` (template
already marks these C3+).

### Step 1 — `k15: library layers 2+3 + catalog`
- Layer 2: `GetOwnedGames` (include_appinfo, played free) → hours/2-week/
  `rtime_last_played` (canonical recency). Layer 3: appdetails (genres,
  categories 28/18 → controller, short_description, metacritic, release) +
  SteamSpy tags w/ votes; ≤1 req/2 s and 1 req/s respectively; cache-forever
  `state/metadata-cache.json`, top-up new appids only.
- Final row schema: `appid, title, installed, hours, lastPlayed, tags(top-10 by
  votes), genres, controller, shortDesc, metacritic, releaseYear`.
- `library.py catalog`: emits the compact prompt rows + prints token estimate.

**Drills:** 3 games spot-checked against the Steam profile; crawl-throttle
timestamps in log; catalog token count vs the 6–18K design envelope.

### Step 2 — `voice: conversation lane (brain + tools)`
- `AnthropicLLMService` behind GrammarGate's no-match path —
  **constructor per pinned docs** (`settings=AnthropicLLMService.Settings(...)`,
  `model="claude-haiku-4-5"`, `enable_prompt_caching=True`; the deprecated
  `model=`/`params=` kwargs are the known trap). Context aggregator pair for
  multi-turn; 60 s cross-session carry; HOLDING window via wake gate.
- System prompt: role + couch rules (spoken lists are summarized — count first,
  top few, compress the tail), catalog block with `cache_control` breakpoint.
- Strict tools calling the **same dispatch module as Tier-1**:
  `launch_game(appid)`, `get_game_details(appid)`, `get_now_playing()`,
  `control(action)`. Client-side validation: appid must exist in the index or
  the tool call is refused. Deps: `+pipecat-ai[anthropic]`.

**Drills:** mech-games Q&A live; "which is shortest?" follow-up (no wake word);
"play it" → launch with validated appid; "volume up" mid-conversation → log
shows GrammarGate swallow, zero LLM call; adversarial: ask it to launch a
made-up appid → tool refused, spoken decline.

### Step 3 — `voice: aura-2 + barge-in + eager EOT`
- Aura-2 WebSocket TTS, sentence-aggregated streaming; **voice audition first**
  (shortlist of `aura-2-*-en` through the actual speaker, pick by ear).
- Barge-in on: interruptions enabled with min-words ≥ 2 / ~250 ms sustained +
  energy floor (config); verify context truncation (next turn coherent).
- Eager EOT on (`eagerEotThreshold` ~0.5): speculative Haiku start, cancel on
  `TurnResumed` (visible in logs); config kill-switch.
- Kokoro fallback: `KokoroTTSService` behind a config switch (manual first;
  auto-fallback-on-TTS-error only if pinned Pipecat offers a clean seam).

**Drills:** TTFA stopwatch ×10 (target ≤1.5 s median, eager ≤1.2 s); barge-in
mid-answer ×5 (playback stops <150 ms by ear, context sane); movie-noise soak
(false barge-in count with guards on); WAN unplugged → Kokoro path or graceful
fail earcon (never a hang).

### Step 4 — `voice: polish + as-built`
- `last_error` watcher speaks launch failures ("The launch failed: host never
  reported ready") — read-only mtime tracking, the chord listener stays the
  marker's sole consumer.
- "what am I playing" wired through `get_now_playing` + title lookup.
- doctor: anthropic auth probe, catalog age/size, TTS reachability.
- As-built: measured latency table, one-week cost readout (Deepgram + Anthropic
  consoles) vs the ~$6–9 budget, the optional model A/B result if run, final
  pins. README final pass. **Project C v2 scope complete.**

---

## Appendices

### A. `config.json` voice section (full schema; commits consume incrementally)
```json
"voice": {
  "inputDeviceName": "",  "outputDeviceName": "",
  "wakeModel": "hey_jarvis",  "wakeThreshold": 0.5,
  "lingerS": 5,  "holdWindowS": 8,
  "eotThreshold": 0.7,  "eagerEotThreshold": 0.5,  "eagerEnabled": true,
  "bargeInMinWords": 2,  "bargeInMinMs": 250,
  "volumeStep": 5,  "volumeMax": 40,
  "keytermCount": 40,
  "ttsVoice": "aura-2-thalia-en",  "ttsLocal": false,
  "assistantModel": "claude-haiku-4-5"
}
```

### B. Secrets timeline
| Key | Needed at | Source |
|---|---|---|
| `deepgramApiKey` | C1 step 3 | console.deepgram.com ($200 credit) |
| `anthropicApiKey` | C3 step 2 | platform.claude.com |
| `steamApiKey` + `steamId64` | C3 step 1 | steamcommunity.com/dev/apikey |

### C. Dependency growth (each pinned at its commit, frozen in as-builts)
| Commit | Adds |
|---|---|
| C1 s0 ✅ | `pipecat-ai[local]==1.7.0` (pyaudio 0.2.14) |
| C1 s2 | `openwakeword==0.6.0` |
| C1 s3 | `pipecat-ai[deepgram]`, `hassil` |
| C2 s3 | `rapidfuzz` |
| C3 s2 | `pipecat-ai[anthropic]` |
| C3 s3 | `kokoro-onnx` (fallback path) |

### D. Risk → owner map (details in design doc §Risks)
| Risk | Owned by |
|---|---|
| Pipecat-on-Windows | C1 s0 ✅ retired (K15 re-confirm at C1 s6) |
| Flux session lifecycle / idle billing | C1 s3 (the flagged unknown) |
| Ex-Link byte slips / query support | C1 s4 drill |
| TV/couch azimuth geometry | C0 (project's top risk) |
| ws-TTS post-barge-in overrun | C3 s3 (sentence chunking bounds it) |
| Tool-call appid hallucination | C3 s2 (strict + index validation) |
| Pipecat upgrade regressions | pins + drill suite as regression tests; upgrades deliberate, never casual |
