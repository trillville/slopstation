# Resuming a running game across sessions — design note

**Status: not implemented.** One attempt shipped on 2026-08-12 and was reverted
the same night. This note exists so the next attempt starts from the evidence
rather than from the same guess.

## The want

End a couch session mid-game, start another, and land back **in the game**
instead of at the Big Picture shell. Today you land at Big Picture and press a
button. That works — this is a convenience, not a fix.

## Why the obvious version failed

`Exit-TV.ps1` closes Big Picture but never quits games, so the game survives
teardown. The display-profile switch on the way out **minimizes** it. The first
attempt read `RunningAppID` in `Enter-TV.ps1` and called `steam.exe -applaunch
<appid>` to pull it back.

It ran a few hundred milliseconds before the ready marker — which is the worst
possible moment in the whole system:

```
00:13:39.11  gamepc  game_resumed appid=1888160     <- -applaunch
00:13:39.11  gamepc  ready
00:13:39.67  k15     host_ready       (+0.56s: couch.py's 1 Hz status poll)
00:13:39.7   k15     exlink_send cmd=hdmi4          <- TV only switches HERE
             …then the S90C's own input-switch latency on top
```

The game re-acquired the display at 2160p while the TV was still showing another
input: black screen, dead controller, and it eventually settled back at Big
Picture anyway.

**The structural lesson: `Enter-TV.ps1` cannot do this correctly at all.** Enter
writes `READY` and exits. It cannot observe the K15's poll, the Ex-Link send, or
the TV. Moving the call "after READY" barely changes anything — READY is when
the K15 *starts* switching, not when the TV is live. Any `Start-Sleep` there is
a guess standing in for a signal Enter does not have.

## What we know

| Fact | Source |
|---|---|
| A game survives teardown and is left minimized | Exit closes BPM only; display switch minimizes |
| Enter has no signal for "TV is live" | writes READY, exits; K15 polls at 1 Hz |
| The K15 **does** — it sends the input switch | `couch.py` `exlink(tvGamingCmd)` |
| `Dispatch.ps1 launch <appid>` answers `ALREADY` for a running game | so it cannot be reused as-is |
| Big Picture as the landing spot works fine | two clean launches, working controller |

## The unknown that gates everything

**Is the black screen a timing artifact, or intrinsic to bringing a
minimized 2160p game back?**

Nothing in the logs distinguishes them, because the game's own re-init is not
instrumented and never will be. If it is intrinsic, no placement fixes it and
the feature is not worth building.

**Resolve this by hand before writing code.** With a session already live and
settled on HDMI 4 — TV showing Big Picture, nothing in transition — minimize the
game, then run `steam.exe -applaunch <appid>` from the desk and watch the TV.

- Comes back fast and clean → timing was the cause, proceed.
- Long black screen anyway → intrinsic. **Stop.** Big Picture is the better
  landing spot and this note is finished.

Two minutes of watching decides whether any of the below is worth doing.

## Candidate designs, if it clears Phase 0

### A — restore the window we already know about *(preferred)*

Steam is not actually needed. The window was minimized by *us*, so capture it on
the way out and restore it on the way in:

1. **Exit**, before switching the display profile — while the game still holds
   the foreground — record `GetForegroundWindow()` and `RunningAppID` to a
   marker file.
2. **Resume** — validate with `IsWindow()` + the appid still running, then
   `ShowWindow(hwnd, SW_RESTORE)`.

No Steam involvement, no launch semantics, no second instance risk — it un-does
exactly the minimize that caused the problem. `Hide-DesktopSteam` already proves
the window-handle machinery works on this hardware. Stale handles are the only
real hazard, and both checks are cheap.

### B — `-applaunch` from the K15, after the input switch

Keep `-applaunch`, but move it to the component that knows when the TV is live:
`couch.py`, immediately after `exlink(tvGamingCmd)`. Needs a new Dispatch verb
(`resume`) because `launch` refuses with `ALREADY`.

Simpler to write than A, but it inherits whatever Steam does on `-applaunch`,
which is the thing that hurt us. Prefer A unless Phase 0 shows `-applaunch` is
clean.

## Guardrails for whichever wins

- **Off the critical path.** Resume must never delay `READY` or the input
  switch. If it hangs, the session is still a session.
- **Config flag, K15-side, default off.** `gaming-pc/` deploys by *copy*, so
  undoing a bad PC-side change is manual. A flag the K15 owns is a cheap kill
  switch.
- **Only on a bare session start.** A voice "play <title>" already has its own
  post-READY launch path; do not stack two.
- **Instrument the outcome, not the intent.** `game_resumed` logged that we
  *asked*. What matters is what ended up in front — the same lesson `fg` on the
  `ready` event was added for.
- **Test the repro deliberately**: game running → end session → start session,
  watched, not inferred from logs afterwards.

## Do nothing is a legitimate outcome

`ready.running_appid` now records whether a game was up at Enter. Let it run for
a couple of weeks first. If starting a session with a game still running turns
out to be rare, the correct amount of engineering here is zero — the current
behaviour (always Big Picture, one button to resume) is predictable and proven.
