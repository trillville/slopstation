# Voice — hardware testing guide

Escalating drills from "audio works" to "full conversation." Each step says
what it proves, what a pass looks like, and what to paste back if it doesn't.
Run everything **on the K15**, in a console you can watch (Ctrl+C to stop). Use
`voice_agent.py` directly for testing — `Start-Voice.bat` is the auto-restart
supervisor for once it works.

## 1. API keys — get these first

Paste them into `k15/secrets.json` (copy from `secrets.template.json`; it's
gitignored, never commit it).

| Key | Where | Unlocks | Needed to start? |
|---|---|---|---|
| `deepgramApiKey` | console.deepgram.com — sign up, no card, $200 credit | STT (Flux) **and** TTS (Aura-2) — i.e. every voice session | **Yes — the gate** |
| `anthropicApiKey` | platform.claude.com → API keys | The assistant lane (Tier-2 conversation, "what mech games…") | For the assistant |
| `steamApiKey` + `steamId64` | steamcommunity.com/dev/apikey (any domain); id from steamid.io | Playtime / recency / tags in the catalog → real recommendations | Enriches assistant |
| `openaiApiKey` | platform.openai.com → API keys | Only the GPT-5.6 A/B (`--provider openai`) | Optional |

**Minimum to begin: Deepgram.** Add Anthropic when you want the assistant.
Steam + OpenAI can come later. Without a real Deepgram key, a wake gets a
wake-tick then the fail earcon (sessions are disabled by design, not a bug).

## 2. Clone on the Desktop + venv

The K15 **runs straight from a git clone** now — no copying. Put the repo on the
Desktop and `git pull` to update:

```
cd %USERPROFILE%\Desktop
git clone <repo-url> slopstation      (or: git pull, if already cloned)
cd slopstation
git checkout main
```

Per-machine files are gitignored — create them once from the committed examples
(in the `k15` folder):

```
copy k15\config.example.json k15\config.json
copy k15\secrets.template.json k15\secrets.json
```

Edit `k15\secrets.json` with your keys (§1). `k15\config.json`'s stable values
(MAC/IP/COM/host) are already right; you'll set device names in §3. Then build
the voice venv (in `k15\voice`):

```
python --version            (want 3.11+; the K15 is 3.13)
python -m venv .venv        (if python isn't on PATH: py -3.13 -m venv .venv)
.venv\Scripts\pip install -r requirements.txt
```

From now on, `git pull` updates the code; your `config.json`/`secrets.json` and
the `.venv` are local and never touched by it. (The chord listener, if running,
is a separate process — voice runs alongside it and can't disturb it. Both lanes
share one Startup shortcut; see **Autostart** below.)

## 2b. Gaming PC side — deploy once

The PC deploys **by copy** (its runtime needs gitignored binaries), so a repo
change there is not live until you copy it. Voice needs the six-verb
`Dispatch.ps1` (`games`/`playing`/`launch` on top of `enter`/`exit`/`status`)
plus `Launch-Game.ps1` — without them `library.py sync` logs `refresh skipped`
and launches never fire. On the **gaming PC**, from its checkout:

```
Copy-Item <repo>\gaming-pc\*.ps1 -Destination C:\CouchGaming\
```

Register the launch task once (runs as you, non-elevated, only when logged on —
a Steam launch needs your interactive session; the 5-minute limit is
load-bearing, same idiom as Enter/Exit):

```
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Launch-Game.ps1'
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'LaunchGame' -Action $a -Settings $s
```

Verify from the K15: `ssh gamepc games` returns JSON (not `DENIED`). That one
reply proves the forced command, the new verbs, and the key all line up.
`Doctor.ps1` on the PC also checks the task and warns on a stale marker.

## 3. Pre-flight — devices + the spike

**Audio devices** — find your mic/speaker and set the config if they aren't the
Windows default:

```
.venv\Scripts\python voice_agent.py --devices
```

Note the `<= default input` / `<= default output` lines. If your array/headset
is the default, leave `config.json`'s `inputDeviceName`/`outputDeviceName` as
`""`. Otherwise set each to a unique fragment of the device name (e.g.
`"ReSpeaker"`). **Paste this output back.**

**The spike** — proves capture + playback + duplex on the K15 (the deferred
"does audio work on this box" confirmation, independent of the cloud):

```
.venv\Scripts\python spike.py
```

Speak a few times → you should hear a beep each time while the `[stats]` lines
keep ticking. Let it run a minute, Ctrl+C, **paste the summary block.** Pass =
capture flowing + duplex tones + no errors.

## 4. Wake word

```
.venv\Scripts\python voice_agent.py --wake-trials
```

First run downloads ~5 openWakeWord model files (one-time, needs internet). Then
say **"hey jarvis"** from where you'll actually sit. Each detection logs a score
and plays the wake tick. **Pass: it fires reliably from your seat, ~9/10, and
doesn't fire on normal talking/TV.** If it's too eager or too deaf, we tune
`wakeThreshold` (0.5 now) — paste the scores. Ctrl+C when done. (`hey_mycroft` /
`hey_rhasspy` are one-line swaps if `hey jarvis` feels wrong; avoid `alexa` near
an Echo.)

## 5. Dry-run full pipeline — the big one (no side effects)

Needs the Deepgram key. `--dry-run` runs the *entire* pipeline — wake → Flux STT
→ grammar → dispatch — but **logs** actions instead of executing them, so it
touches neither the TV nor the PC.

```
.venv\Scripts\python voice_agent.py --dry-run
```

Say "hey jarvis" (tick), then try:
- "volume up" · "mute" · "switch to the apple tv" → one earcon + a
  `DRY-RUN would: …` line
- "start a session" → `DRY-RUN would: couch.py start`
- "what mech games do I have" → no command match (fail earcon unless the
  Anthropic key is in, in which case it goes to the assistant)
- "thanks" → soft close earcon, session ends

Watch `..\couch.log` (or the console) for `[voice]` lines. **This proves STT +
grammar + earcons end-to-end.** Paste a chunk of the log. This is where you'll
feel the rhythm — wake-to-tick gap, how long after you stop talking it reacts,
whether phrasings you'd naturally use actually match. Jot down any that don't.

## 5b. Natural sentences — the pre-roll buffer

No pause needed anymore: the wake stream keeps recording through the session
build (seeded with the 2 s before detection), replays it into Flux ahead of
live mic audio, and the gate strips the wake phrase from the transcript. Say
each as ONE flowing sentence, no gap after "jarvis":

- "hey jarvis volume up" → log shows `pre-roll: feeding N.Ns …` then
  `wake prefix stripped: "hey jarvis volume up" -> "volume up"` → `VolumeUp`
- "hey jarvis play armored core six" → same, longer sentence — the command
  words span the build gap, this is the stress case
- "hey jarvis" alone, pause, then "volume up" → the old style must still work:
  first turn logs `wake phrase only, listening`, second turn matches
- "hey jarvis what mech games do I have" → strip + assistant lane

Pass = the strip line appears and the intent matches, both styles. If a long
sentence loses a syllable ~2 s in, that's the residual ~100–200 ms capture→mic
handoff gap — paste the transcript it produced and we'll move the handoff into
the transport.

## 6. Ex-Link TV frames — careful, TV on

Volume/mute go over the serial cable to the TV. **A one-byte slip in this family
is `power_off`**, so these frames are frozen and computed — send only via the
named commands. TV on, from `k15`:

```
..\voice\.venv\Scripts\python exlink.py vol_up
..\voice\.venv\Scripts\python exlink.py vol_down
..\voice\.venv\Scripts\python exlink.py mute_toggle
..\voice\.venv\Scripts\python exlink.py vol_set 20
..\voice\.venv\Scripts\python exlink.py probe_volume
```

(run from the `k15` folder, or `python exlink.py …` with its own interpreter).
Each send should print `ack 030cf1` and move the TV — acks are validated, so
a `FAILED` line means the command really didn't land. **The probe question is
settled** (decode drill, 2026-08-10): the S90C acks status queries but answers
with a constant canned echo — byte-identical at volume 7/23, muted/unmuted —
so there is no readable state, mute stays a blind toggle, and `vol_set` is the
resync. `probe_volume`/`decode_volume` remain as bench tools; this section is
DONE once the five sends above ack and move the TV.

## 7. Live commands — real dispatch

Drop `--dry-run`. Now actions execute.

```
.venv\Scripts\python voice_agent.py
```

- "volume up" / "mute" / "volume 15" → the TV moves
- "switch to the playstation" → input changes instantly
- "switch to the pc" with no session → **starts one** (same as "start a
  session" — it means "get me gaming"); mid-launch → "still starting"; with a
  live session → flips instantly
- "start a session" → the full couch launch fires (TV untouched until READY)
- "end session" mid-game → teardown runs — **the exit-by-voice moment**

Confirm the chord still works too (they're independent). Paste `couch.log`.

## 8. Library + game launch

Fill the index (needs the PC awake; Steam layers need the Steam key):

```
.venv\Scripts\python ..\library.py sync
.venv\Scripts\python ..\library.py show
```

`show` should list your installed games. Then, in a live session:

- "play armored core six" → launches into Big Picture
- "play <something not installed but owned>" → spoken "not installed" decline
- "play <a game>" while another runs → "quit it first"
- fuzzy torture: "play armored core", partial names — note misses

## 9. The assistant

Needs the Anthropic key. **Iterate text-first** (no audio, instant, dry-run —
actions log, never fire):

```
.venv\Scripts\python voice_agent.py --text
```

Type: "what mech games do I have" · "which is shortest" · "suggest a roguelike I
haven't played in a while" · "play the second one". Watch the tool calls print
and the latency per answer. (Catalog is thin until step 8's `sync` runs the
Steam layers — recommendations need the tags.) When it reads well, go live:

```
.venv\Scripts\python voice_agent.py
```

"hey jarvis … what should I play tonight?" — spoken answer, follow-ups without
re-waking, "play it" launches. This is the whole thing working.

## 10. The A/B (optional) — Haiku vs GPT-5.6

Needs the OpenAI key. Same prompts, both providers, watch latency + quality:

```
.venv\Scripts\python voice_agent.py --text --provider anthropic
.venv\Scripts\python voice_agent.py --text --provider openai --effort low
.venv\Scripts\python voice_agent.py --text --provider openai --effort medium
```

Reasoning happens *before* the first spoken word, so effort trades latency for
depth — that's the tradeoff to feel. Flip `config.assistantProvider` to move
production to the winner.

## Autostart — one shortcut starts everything

`Start-K15.bat` is the single Startup-folder shortcut target: it launches the
listener supervisor and the voice supervisor in their own minimized windows
and exits (Autologon makes login = boot). Both supervisors are
**single-instance** — an fd-9 handle on a lock file, held for the window's
lifetime — so a stray old shortcut or a manual run can't double-listen on the
Puck or fight over the mic. `Start-K15.bat` probes those same locks, so it is
safe to re-run: it starts only the lanes that are actually down. One paste on
the K15 removes any old per-lane shortcuts and installs the one shortcut:

```
$startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"; $sh = New-Object -ComObject WScript.Shell; Get-ChildItem "$startup\*.lnk" | Where-Object { $sh.CreateShortcut($_.FullName).TargetPath -match 'Start-(Listener|Voice|K15)\.bat$' } | ForEach-Object { "removing $($_.Name)"; Remove-Item $_.FullName }; $sc = $sh.CreateShortcut("$startup\Start-K15.lnk"); $sc.TargetPath = "$env:USERPROFILE\Desktop\slopstation\k15\Start-K15.bat"; $sc.WorkingDirectory = "$env:USERPROFILE\Desktop\slopstation\k15"; $sc.WindowStyle = 7; $sc.Save(); "installed Start-K15.lnk"
```

Day to day: close a window to stop that lane (the supervisors only
auto-restart crashes, not closes); for bench sessions close the voice window
first, then run manually with flags. **After a `git pull`, run
`Start-K15.bat --restart`** — a running agent holds the old modules in memory,
and `--restart` kills the agents (never the supervisor windows) so each
supervisor relaunches its own on the new code after its 10 s backoff. A live
session is undisturbed: `couch.py reconcile` runs outside that loop, so
bouncing the listener agent can't re-trigger it against a session already
being watched. Remove `Start-K15.lnk` to undo autostart.
Verify the unattended chain once: reboot, touch nothing — chord a session,
then "hey jarvis volume up".

## Mic array bring-up — when the ReSpeaker arrives

Everything above works on any mic; the array is what makes wake reliable from
the couch at movie volume. This is the acoustic gate — the project's top risk,
decided by data, not taste.

1. **Prep:** USB3/xHCI port; Zadig WinUSB on the control interface;
   `xvf_host VERSION`; confirm UA/USB 2-ch 16 kHz firmware; check speaker-out
   audibility at couch distance; run `spike.py` for a 10-minute soak on the
   array. `Start-Voice.bat`'s `xvf_host REBOOT 1` line un-gates itself once
   `voice\xvf_host\xvf_host.exe` exists (reboot-hang workaround).
2. **Aim** (candidate 1: atop the console): `AEC_FIXEDBEAMSONOFF 1` → set both
   azimuths/elevations at couch-left/right (slight up-tilt) →
   `AEC_FIXEDBEAMSGATING 1` → validate from each seat via `AEC_SPENERGY_VALUES`
   + the LED DoA → only after the live config proves out, `SAVE_CONFIGURATION 1`
   **once**. Learn Safe-Mode recovery first (hold mute at boot) — the brick bug
   is real.
3. **Trials:** `voice_agent.py --wake-trials` 20× per condition
   {movie volume, loud movie} × {couch-left, couch-right}; then
   `--false-accept-soak` through one ~2 h movie.
4. **Gate:** ≥18/20 in every condition AND ≤1 false accept per movie. Record
   the placement, azimuths, and the full table in
   [voice-assumptions.md](voice-assumptions.md). Miss ⇒ try candidate 2
   (in-cabinet, foam, against the grate) ⇒ still miss ⇒ add a beam-energy
   double-gate to the wake loop ⇒ still miss ⇒ stop and reassess mount
   geometry.

## Fastest path to "it works"

Keys (Deepgram) → §2 setup → §3 devices+spike → §4 wake → **§5 dry-run** (proves
voice end-to-end, safely). Everything after §5 is making real things happen.

## What to paste back if something's off

`--devices` output · the spike summary · wake-trial scores · a `couch.log` chunk
around the failing session · the `probe_volume` result · and the exact console
error if anything throws. The whole system logs `[voice]` lines to `couch.log`
next to `config.json`.
