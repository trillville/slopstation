# Resuming a running game across sessions — design note

**Status: not implemented, and for exclusive-fullscreen games it should not be.**
One attempt shipped on 2026-08-12 and was reverted the same night. Phase 0 was
answered on 2026-08-13 (below) and it rules out both candidate designs for that
case. This note exists so the next attempt starts from the evidence rather than
from the same guess.

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
| What breaks is the graphics DEVICE, not the window | 2026-08-13: black screen with a live mouse cursor drawn on top of it |
| Steam's Resume on a running game just foregrounds the process | which is exactly where design B lands too |
| A game running at Enter is a real occurrence, not a hypothetical | `ready.running_appid=1888160`, twice inside five minutes |

## Phase 0, answered — 2026-08-13

The gating question was whether the black screen is a timing artifact or
intrinsic to bringing a displaced game back. For a game in exclusive fullscreen:
**intrinsic.**

It was run by accident rather than on purpose, under conditions close enough to
count. AC6 (`1888160`) had been playing at the desk in exclusive fullscreen, was
minimized, and was left running. Chord at 14:01:59 (`turn=2c7936`) — the launch
was clean end to end and the TV was settled on HDMI 4 showing Big Picture, with
nothing in transition:

```
14:02:08  gamepc/enter  profile_applied retried=False profile=TV-GAMING
14:02:18  gamepc/enter  ready fg=Steam Big Picture Mode running_appid=1888160 focused=True
14:02:19  k15/launch    host_ready dur_ms=19764 verified=True
```

Steam's own Resume button, pressed from that settled session, gave a long black
screen with a visible mouse cursor.

**The cursor is the diagnosis.** It is drawn by the compositor, so the desktop
was alive and painting on the TV — only the game's surface was dead. What breaks
is the graphics DEVICE, not the window: an exclusive-fullscreen swapchain is
bound to an output and a mode, and TV-GAMING replaces both underneath it.
Recovery is the game's own device-lost handler or nothing. No external process
can do it, which is why no placement of any call fixes this.

One honest caveat: this is a harsher case than the test this section used to
prescribe. AC6 had never rendered on the TV at all, so it had to initialise onto
a mode it had never held rather than re-acquire one it had. That does not change
the verdict for exclusive fullscreen, and it is also the more common shape — a
game left running at the desk when someone moves to the couch.

### What is still open

**Does a BORDERLESS game survive the same switch?** A borderless window is an
ordinary desktop window: a resolution change resizes it through normal window
messages instead of destroying a device. It should survive, and its failure mode
degrades from a black screen to a mis-sized window — visible, and fixable.

Untested, and it decides everything below. Most of the library is borderless
now, so if it holds, exclusive fullscreen is AC6-shaped rather than systemic and
the per-game setting is the entire fix.

## Candidate designs — both refuted for exclusive fullscreen

Kept in full, with their verdicts, because both are the obvious idea and will be
had again.

### A — restore the window we already know about *(was preferred; dead here)*

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

**Verdict: it un-does the wrong thing.** The minimize was never the problem —
the lost device was. `SW_RESTORE` gives back a window whose surface still cannot
paint, which is the same black screen by a cheaper route. Only worth revisiting
for borderless games, and if borderless survives on its own there is nothing
left for it to fix.

### B — `-applaunch` from the K15, after the input switch

Keep `-applaunch`, but move it to the component that knows when the TV is live:
`couch.py`, immediately after `exlink(tvGamingCmd)`. Needs a new Dispatch verb
(`resume`) because `launch` refuses with `ALREADY`.

Simpler to write than A, but it inherits whatever Steam does on `-applaunch`,
which is the thing that hurt us. Prefer A unless Phase 0 shows `-applaunch` is
clean.

**Verdict: already tested by hand, and it failed.** Steam's Resume button on a
running game foregrounds the existing process, which is where `-applaunch`
lands too — so 2026-08-13 ran this design's end state manually, from a settled
session, with the timing objection removed. Black screen. Correct placement
cannot save a call whose destination is broken.

## Guardrails, if the borderless case ever revives this

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

## Do nothing is now the outcome, not just a legitimate one

For exclusive fullscreen the right amount of engineering is zero, because there
is nothing to engineer: no external process can recreate another process's lost
graphics device. The two levers that do work are outside this system — the
game's own display setting, and quitting before moving rooms, which lands the
game on the TV at 2160p from a fresh start. That path has never failed.

`ready.running_appid` keeps counting how often a game is up at Enter. If the
borderless question above resolves clean, finish this off: move one line to
README § *Deliberately not doing* and delete this note.
