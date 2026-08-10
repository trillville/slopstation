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

(rows appended as the build proceeds)

## Open questions

(appended as they arise; each gets an owner: a drill, a key, or a decision of yours)
