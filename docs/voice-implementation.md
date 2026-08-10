# Voice Control — implementation plan (living document)

Architecture is settled in [voice-control-design.md](voice-control-design.md) —
this file is about *building* it. Rules of the file: the **current phase** is
planned in commit-by-commit detail; **completed phases** collapse to short
as-built records (gate results, deviations kept, plans deleted); **future
phases** stay as one-liners until their turn — C2/C3 inherit version pins and
artifacts from C1, so planning them early would just be guessing in markdown.

## Status ledger

| Phase | State | Gate result |
|---|---|---|
| **C1 — spike + command lane** | **current — planned below** | — |
| C0 — acoustic gate | protocol below; **blocked on array delivery**, consumes C1 steps 0–2 (the bench kit) | — |
| C2 — game launch | unplanned (needs C1's grammar + pins); scope in design doc | — |
| C3 — conversation lane | unplanned (needs C1 spike verdict + C2 index); scope in design doc | — |

Division of labor, as ever: Claude ships code + exact drills; Tillman runs them
on real hardware and pastes results. Every step ends with a paste-back.

---

## C1 — Pipecat spike + command lane (current)

**Exit criteria:** every command class works by voice end-to-end on the K15 —
session start/end, volume/mute, input switching — at ≤0.5 s end-of-speech →
earcon, with the chord listener demonstrably unaffected, surviving a supervisor
kill/restart, and a pinned dependency set recorded here.

### Step 0 — pre-flight + the spike (the gate for everything above it)

User pre-flight on the K15 (paste all output back):

```powershell
python --version
py -0p
```

- Need ≥3.11 for Pipecat. If missing: install Python 3.12 from python.org with
  **"Add to PATH" UNCHECKED** — the chord listener resolves `python` from PATH
  and must keep resolving to what it runs today. The voice venv will reference
  the new interpreter explicitly (`py -3.12 -m venv`).
- Sign up at console.deepgram.com ($200 credit, no card); put the key in
  `k15\secrets.json` per [secrets.template.json](../k15/secrets.template.json).
  (Anthropic key: not until C3. Steam key: not until C2.)

Then the spike: Claude ships `k15/voice/spike.py` + `requirements.txt` pinned to
the current Pipecat release — a ~40-line pipeline (`LocalAudioTransport` →
Silero VAD → console prints + a tone out the speaker on speech start) that
proves mic capture, speaker playback, and VAD all work on Windows/PyAudio with
any USB mic. User runs:

```powershell
py -3.12 -m venv C:\Users\minipc\Desktop\voice\.venv
C:\Users\minipc\Desktop\voice\.venv\Scripts\pip install -r C:\Users\minipc\Desktop\voice\requirements.txt
C:\Users\minipc\Desktop\voice\.venv\Scripts\python C:\Users\minipc\Desktop\voice\spike.py
```

**Pass:** device list prints, speech → `[vad] speaking` lines + audible tone,
10 minutes without device errors. **Fail path A:** PyAudio/WASAPI device
errors → Claude writes the thin `sounddevice` transport (~200 lines, design
doc's escape hatch), re-run. **Fail path B (catastrophic):** frame pipeline
itself misbehaves on Windows → stop; hand-roll decision per design doc. The
verdict and final pins get recorded in the as-built when this collapses.

### Build order (one commit each, `py_compile` + drill per commit)

1. **`voice: scaffold + spike harness`** — `k15/voice/` (spike.py,
   requirements.txt pinned, Start-Voice.bat skeleton: venv-activate + restart
   loop, no array steps yet), README rows. *Drill: the spike above.*
2. **`voice: wake gate + bench modes`** — `wake_gate.py` (OpenWakeWordGate
   FrameProcessor, oWW ONNX on `hey jarvis`, model auto-fetch to `voice/models/`
   on first run), tick earcon via `earcons.py` (synthesized at startup, count
   vocabulary matching the thuds), and two bench modes on `voice_agent.py`:
   `--wake-trials` (log each detection with confidence; the C0 protocol's
   instrument) and `--false-accept-soak` (count spurious wakes over hours).
   *Drills: 10 wakes from ~10 ft (quiet room, any mic — this is a code check,
   not the acoustic gate); 1-hour TV soak, count false accepts.*
3. **`voice: flux session + grammar gate`** — Flux socket opened on wake /
   closed at DORMANT, `grammar_gate.py` + `grammar.yaml` (hassil: start/end
   session, volume up/down/set/mute, input switching, exit phrases; `end
   session` exact-match-only), earcon acks, LINGER window, lock-arbiter check
   before start (busy earcon mirrors the chord's 2-thud rule). Dispatch stubbed
   to log-only this commit. *Drills: transcript unit table
   (`voice/test_grammar.py`, runs offline); live: each template phrased 3 ways,
   watch `[voice]` lines in couch.log; "volume up" must show NO LLM anywhere
   (there is none yet — the log proves the lane).*
4. **`k15: exlink volume/mute frames`** — cglib gains the checksum builder +
   frozen frames (vol_up `082201000100d4`, vol_down `082201000200d3`,
   mute_toggle `082202000000d4`, parametric vol_set), `volumeMax` clamp,
   exlink.py CLI picks them up automatically. *Drills (TV on, from K15):
   `python exlink.py vol_up` etc., expect `030cf1` acks + visible TV response;
   one 500 ms read probe of the volume query frame — answer decides whether
   mute state is real or software-tracked. Frames from Samsung's official
   worksheet; never hand-typed.*
5. **`voice: dispatch wiring`** — GrammarGate stubs → real: `couch.py start`
   spawn (same pattern as the listener), `ssh gamepc exit`, cglib exlink calls.
   *Drills: full command matrix — voice session start from cold (one rule
   intact: TV untouched until READY), voice "end session" mid-game (the exit
   asymmetry, finally closed), volume/mute/input in every system state,
   chained commands inside LINGER.*
6. **`voice: supervisor + doctor + docs`** — Start-Voice.bat finalized (array
   REBOOT step added later, gated on xvf_host presence so any-mic setups
   no-op), doctor.py voice rows (venv + pins present, mic device found, wake
   model loaded, Deepgram auth ping, Flux reachable, grammar parses), README +
   this file's as-built. *Drills: kill voice python.exe → auto-restart ≤10 s;
   kill it during a session → listener/chord unaffected (separate venv,
   separate process — prove it); doctor PASS run + one induced FAIL (rename
   secrets.json).*

**C1 deploy model:** copy `k15/voice/` folder + updated `cglib.py`,
`config.json` (new `voice` section), `doctor.py`, `exlink.py` to the K15
Desktop; venv is created on-machine (step 0 commands) — never copied.

**Rollback:** voice is additive; close the Start-Voice window and the system is
byte-for-byte pre-C1. cglib's exlink additions are new dict keys + one builder —
inert to existing callers.

---

## C0 — acoustic acceptance gate (execute when the array arrives)

Consumes step 2's `--wake-trials` / `--false-accept-soak` bench modes. Protocol:

1. **Prep:** plug into a USB3 (xHCI) port; Zadig WinUSB on the control
   interface; `xvf_host VERSION` smoke; confirm UA/USB firmware (2-ch, 16 kHz);
   speaker-out audibility check (earcon through the array's 5 W speaker at
   couch distance over TV audio).
2. **Aim (placement candidate 1: atop console, cable through grate):**
   `AEC_FIXEDBEAMSONOFF 1` → both azimuths/elevations at couch-left/right
   (measured, radians, slight up-tilt) → `AEC_FIXEDBEAMSGATING 1` → validate by
   speaking from each couch seat watching `AEC_SPENERGY_VALUES` + LED DoA mode →
   only after live config is proven: `SAVE_CONFIGURATION 1` **once** (know the
   Safe-Mode recovery chord — hold mute at boot — before saving; the brick bug
   is real).
3. **Trials:** `--wake-trials`, 20 attempts per condition: {movie volume, loud
   movie} × {couch-left, couch-right}. Then `--false-accept-soak` through one
   full ~2 h movie.
4. **Gate:** ≥18/20 per condition and ≤1 false accept per movie → **pass,
   record placement + azimuths here, C0 done.** Miss → candidate 2 (in-cabinet
   on foam pad against the grate) → repeat. Still miss → enable the beam-energy
   double-gate in wake_gate.py → repeat. Still miss → stop and reassess per the
   design doc's top risk row (geometry may demand a different mount point).
5. Record the results table (condition × hits) in the as-built regardless of
   outcome — C3's barge-in guard tuning wants this data.

---

## C2 / C3 — deliberately unplanned

Scoped in the design doc (§Phases). Each gets its detailed plan here when its
predecessor's gate closes: C2's plan will name the exact Dispatch/`Launch-Game`
diffs and library.py layer-1 schema against C1's real grammar; C3's will pin
the Anthropic/Aura service configs against whatever Pipecat version C1 proved
(constructor signatures from the pinned version's docs, not tutorials — the
`Settings` pattern note in the design discussion applies).
