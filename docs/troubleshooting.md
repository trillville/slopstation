# Troubleshooting — when it doesn't work at 9pm

Symptom → diagnosis → fix, for both lanes. The chord lane is load-bearing; the
voice lane is an overlay and its failures never affect the chord.

**First move, always:** tail `k15/couch.log` (next to `config.json`). Every
process tags its lines — `[listener]`, `[launch]`, `[voice]`, `[library]`,
`[supervisor]`. `python doctor.py` on the K15 and, on the PC:

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Doctor.ps1
```

Each diagnoses its whole chain read-only. The `-ExecutionPolicy Bypass` is not
optional and not a workaround: this box's policy is `Restricted` (the Windows
client default), so a bare `C:\CouchGaming\Doctor.ps1` fails with
`running scripts is disabled on this system`. Every scheduled task and the sshd
forced command already invoke PowerShell exactly this way — so running it by
hand like this is also the closest match to how the real thing runs.

**When one launch is the question**, the same events are also in
`k15/logs/k15-YYYYMMDD.jsonl` with a `turn` id that follows a single intent
from the wake word or the chord all the way to the gaming PC (whose half is in
`C:\CouchGaming\logs\pc-YYYYMMDD.jsonl`, and whose transcript filename carries
the same id). One launch, both machines:

```bash
grep '"turn":"9f2c1a"' k15/logs/k15-*.jsonl
```

## Chord lane

| Symptom | Diagnosis | Fix |
|---|---|---|
| Chord does nothing, no launch console | RDP to K15, tail `couch.log` | No `[listener]` lines → listener not running: run `.\Start-K15.bat` (safe any time — it starts what's down and reloads what's up). `already active/starting - ignoring` → lock is fresh: a session is live, or a crash happened <5 min ago — wait it out or `del state\session.lock` |
| Listener says `armed` but the chord never fires | Controller firmware update moved the report bytes | `python calibrate.py`, update `RID_INPUT`/`BTN_BYTE`/`CHORD` in `chord_listener.py` |
| End TV Session tile does nothing | `schtasks /Run /TN \CouchGaming\Exit` says `currently running` — wedged instance | `schtasks /End /TN \CouchGaming\Exit`; the 5-min execution limit also self-clears it |
| Launch aborts with `stale Puck claim would not release` | The old claim wouldn't die, so launching would give a dead controller | RDP to K15: reseat the Puck's USB or restart the VirtualHere service; at the desk check the vhui64 client |
| Controller connects but nothing works: every button plays a navigation chime, nothing moves on the TV, and the Steam button still opens a menu you then can't navigate | Enter focused the **desktop Steam window** instead of Big Picture, and Steam delivers input to the focused window (the Steam button works regardless — Steam Input handles it globally, which is what makes this so confusing). The `ready` event names the window that actually held the foreground: `fg='Steam'` is the failure (and now forces `focused=False` + warn level), `fg='Steam Big Picture Mode'` or a game title is healthy. Same line in the transcript: `READY (foreground: '…')`. The trigger is **a game left running by the previous session** — Exit closes Big Picture but never quits games, so Big Picture has no window when Enter looks for it | Click the Big Picture window on the TV — the session recovers in place, no relaunch needed. Quitting the game before launching also avoids it. Fixed in Enter (the `'Steam'` fallback that caused it is gone) |
| TV switches but shows a black/garbled screen | Enter transcript didn't end `READY`, or the profile failed after READY | Read the newest `enter-*.log`; Ctrl+Alt+E to tear down; check the TV-GAMING profile still applies by hand (guide Stage 6) |
| TV stuck on HDMI 4 after a session ended | K15 lost the session (rebooted mid-session, or the watch died) | Check `couch.log` tail; reboot the K15 (`reconcile` cleans up); the TV remote is the manual fallback |
| Session ends by itself mid-game, TV off | `couch.log`: `gaming PC gone` after 3 failed polls — transient network/sshd outage ≥ ~30 s | Ctrl+Alt+E at the desk, then re-chord. If it recurs, raise `WATCH_FAILS` in `couch.py` |
| `FAILED:1` repeating during a launch | PC is at the login screen — no interactive session, so the Enter task can't start | Log in at the PC; couch launches from **sleep** (the supported couch-ready state) don't hit this |
| `office-safety` log says `standing down` | An Enter/Exit was genuinely running at logon | Normal. If it repeats every logon, check for a wedged task: `schtasks /Query /TN \CouchGaming\Enter /FO LIST` |
| Keyboard wake doesn't clean up a stale session | `wake-safety-*.log`'s raw `powercfg /lastwake` dump doesn't match the pattern | Widen `$NetworkWakePattern` in `Wake-Safety.ps1` to match your NIC's actual string |
| Enter/Exit transcript shows `Log is not recognized` | `CouchGaming.common.ps1` missing or a partial copy | Re-copy all of `gaming-pc/` to `C:\CouchGaming\` |

## Voice lane

| Symptom | Diagnosis | Fix |
|---|---|---|
| No wake at all | No `[voice] wake` lines in `couch.log` | Is the supervisor window open? `python doctor.py` → the voice rows report agent/venv/keys. Wrong mic bound: `--devices` and set `inputDeviceName` |
| `another Start-Voice window is already running` | Single-instance guard — a startup copy is live | Close that window first (it's the off switch; the supervisor only auto-restarts crashes) |
| Wake fires, then nothing | `session crashed:` in the log | Whatever follows is the real error; the agent returns to dormant either way. A missing/placeholder `deepgramApiKey` disables sessions by design (startup logs it) |
| Question heard, no spoken answer | `passing to assistant` then silence until `idle` | Two causes. **API error** (bad key, no model access): now mirrored into `couch.log` as `pipeline error: …`; the agent console has the full loguru detail. **Slow model**: the idle handler defers up to 30 s for an in-flight answer, so a reasoning model thinking past `holdWindowS` no longer gets its session killed. Isolate with the REPL (`--text --provider …`) — same key/model/prompt, no pipeline |
| `wake stream died (…) - rebuilding audio in 5s` | Mic stream death (BT profile churn), or 30 s of literal zeros — a zombie stream whose endpoint vanished without an error | Self-healing: the agent rebuilds PortAudio from scratch and re-resolves both devices by name; the log then shows `input device: '…'` — check it bound the mic you expect. If constant, move off Bluetooth — see the next row. A hardware-muted mic also reads as zeros (rebuild loop every 30 s, harmless) |
| Audio flapping / `-9999` on Bluetooth (AirPods) | HFP/A2DP endpoint split: Windows exposes BT input on the Hands-Free device and output on the A2DP "Headphones" device, and the two profiles are mutually exclusive. A session's held mic keeps HFP active, so the first playback tries to wake the suspended A2DP endpoint | Point BOTH `inputDeviceName`/`outputDeviceName` at the "Headset" endpoints — one profile, no flapping, phone-quality output. Bluetooth is a degraded test rig, not a target: wired/USB (the array) has none of this |
| A phrase never matches | The log shows `heard "…" - passing to assistant` | The grammar is deliberately narrow for risky commands. Add the phrasing to `voice/grammar.yaml` and re-run `tests/test_grammar.py` |
| `play <title>` finds nothing | `no confident title match`, or an ambiguity refusal (near-ties refuse on purpose) | `python library.py sync` then `show`; say more of the title. The index needs the PC awake for the installed layer |
| `library refresh skipped (… returned non-zero exit status 1)` | The PC's `Dispatch.ps1` is older than the six-verb version — `games` returns `DENIED` | Re-deploy the PC side (voice-testing.md §2b) |
| Launch says OK but no game starts | Big Picture up, game never asked for | Read the newest `C:\CouchGaming\logs\launchgame-*.log`. A `Remove-Item … Access denied` there means the marker/token fix isn't deployed — re-deploy `Dispatch.ps1` + `Launch-Game.ps1` |
| TV command silently does nothing | `exlink … FAILED` in the log (acks are validated: `030cf1` or it didn't land) | TV off/asleep, or COM contention. `python exlink.py vol_up` to test the port directly |
| Mute state feels backwards | Mute is a **blind toggle** — the S90C acks its status query but answers with a constant canned echo, byte-identical muted or not, so there is no state to read | Say a volume number ("volume 20") — an absolute set is the resync |
