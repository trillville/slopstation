# v4 → v5 Migration Guide

The v5 refactor is behavior-preserving with deliberate, documented exceptions
(see the commit log from tag `v4.4` to HEAD). Deploy is a file copy — no task
re-registration, no sshd/VirtualHere/config changes.

## What lands on each machine

**Gaming PC** — all 6 files from `gaming-pc/` → `C:\CouchGaming\`:

- `CouchGaming.common.ps1` **(new — required; the scripts fail without it)**
- `Enter-TV.ps1`, `Exit-TV.ps1`, `Office-Safety.ps1`, `Wake-Safety.ps1`, `Dispatch.ps1`

**K15** — 7 files from `k15/` → `C:\Users\minipc\Desktop\`:

- `cglib.py` **(new — required)**
- `couch.py`, `chord_listener.py`, `exlink.py`, `calibrate.py`
- `Start-Listener.bat` (changed: runs `reconcile` first), `Start-TV-Gaming.bat` (unchanged, copy anyway)
- `config.json` stays as it is — do **not** overwrite it if you've changed values locally

## Steps

1. **Gaming PC** (no session active): copy the 6 files over `C:\CouchGaming\`.
2. **K15**: close the running listener console first (the old process holds the
   Puck's HID handles), then copy the 7 files over the Desktop.
3. **Reboot the K15** — this exercises the real unattended startup chain,
   including the new reconcile step.

## Validation (~20 min, in order)

| # | Test | Expect |
|---|---|---|
| T0 | Gaming PC, normal PowerShell: `. C:\CouchGaming\CouchGaming.common.ps1` then `Get-PrimaryHeight`, `Test-PuckPresent`, `Test-CgTaskRunning Enter` | Ultrawide height (e.g. 1440) · `False` · `False` |
| T1 | Task Scheduler → run `Enter`, then `Exit` (TV on, at desk) | TV-only + Big Picture + controller; then office + Puck released. Transcripts end `READY` / `Puck released` |
| T2 | From K15: `ssh gamepc status`, `ssh gamepc bogus` | `NOTREADY` · `DENIED` |
| T3 | **The real one**: PC asleep + TV off → chord → play → End TV Session tile | One input transition into Big Picture; teardown restores desk + TV off; listener re-arms. `couch.log` shows `[launch]` and `[listener]` lines |
| T4 | Run `Enter` from Task Scheduler; mid-launch press Ctrl+Alt+E | Exit log: `Enter task is running - stopping it (teardown wins)`; clean office after |
| T5 | K15, no session: create a lock and age it, then run `Start-Listener.bat`:<br>`python -c "import pathlib,time; p=pathlib.Path('state/session.lock'); p.parent.mkdir(exist_ok=True); p.write_text(str(time.time()))"`<br>`(Get-Item .\state\session.lock).LastWriteTime = (Get-Date).AddMinutes(-6)` | Log: `reconcile: stale lock from a dead session - clearing, TV untouched`; listener arms |
| T6 | Reboot the gaming PC | An `office-safety-*.log` appears in `C:\CouchGaming\logs\` |
| T7 | Mid-session, pull the gaming PC's Ethernet (or force-sleep it) | Within ~30 s the K15 logs `gaming PC gone (slept/crashed) - treating as ended` and restores the TV — newly working in v5; v4 never actually detected this |

## Troubleshooting

| Symptom | Diagnosis | Fix |
|---|---|---|
| Enter/Exit transcript shows `Log is not recognized` or a dot-source error | `CouchGaming.common.ps1` missing or partial copy | Re-copy all 6 files |
| Chord does nothing, no launch console | RDP to K15, tail `couch.log` | No `[listener]` lines → listener not running: run `Start-Listener.bat` (check Task Manager for a duplicate python first). `already active/starting - ignoring` → lock is fresh: a session is live, or a crash happened <5 min ago — wait it out or `del state\session.lock` |
| Listener says `armed` but the chord never fires | Controller firmware update moved the report bytes | `python calibrate.py`, update `RID_INPUT`/`BTN_BYTE`/`CHORD` in `chord_listener.py` |
| End TV Session tile does nothing | `schtasks /Run /TN \CouchGaming\Exit` says `currently running` — wedged instance | `schtasks /End /TN \CouchGaming\Exit`; the 5-min execution limit also self-clears it |
| Launch aborts with `stale Puck claim would not release` | v5's D1 guard: the old claim wouldn't die, so launching would give a dead controller | RDP to K15: reseat the Puck's USB or restart the VirtualHere service; at the desk check the vhui64 client |
| TV switches but shows a black/garbled screen | Enter transcript didn't end `READY`, or profile failed after READY | Read the newest `enter-*.log`; Ctrl+Alt+E to tear down; check TV-GAMING profile still applies by hand (guide Stage 6) |
| TV stuck on HDMI 4 after a session ended | K15 lost the session (rebooted mid-session, or watch died) | Check `couch.log` tail; reboot K15 (reconcile cleans up); TV remote is the manual fallback |
| Session ends by itself mid-game, TV off | `couch.log`: `gaming PC gone` after 3 failed polls — transient network/sshd outage ≥ ~30 s | Ctrl+Alt+E at desk, then re-chord. If it recurs, raise `WATCH_FAILS` in `couch.py` |
| `FAILED:1` repeating in `couch.log` during a launch | PC is at the login screen — no interactive session, so the Enter task can't start | Log in at the PC; couch launches from **sleep** (the supported couch-ready state) don't hit this |
| `office-safety` log says `standing down` | An Enter/Exit was genuinely running at logon | Normal. If it repeats every logon, check for a wedged task: `schtasks /Query /TN \CouchGaming\Enter /FO LIST` |
| Keyboard wake doesn't clean up a stale session | `wake-safety-*.log`'s raw `powercfg /lastwake` dump doesn't match the pattern | Widen `$NetworkWakePattern` in `Wake-Safety.ps1` to match your NIC's actual string |

## Rollback

```
git checkout v4.4 -- gaming-pc k15
```

Copy those files back over both machines (they're self-contained — no lib
needed) and restore the old `Start-Listener.bat`. The K15 and PC can run mixed
v4/v5 without breaking the SSH contract in either direction.
