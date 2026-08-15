# Voice-expansion bring-up & live test

The new voice surface — richer Steam questions, Big Picture navigation, quitting
a game, collections, and install-by-voice — built end-to-end and green on the
blind suite. This is the on-hardware drill that turns "the tests pass" into "it
works on the couch." Escalating, like `voice-testing.md`: everything harmless
first, the TV-facing and account bits last. Someone should be at the TV for
Stages 3–5, because any of them can change what's on screen.

Read the whole of a stage before running it. Where a result is still an open
question, the drill says so and says what it decides — those are the Phase-0
gates, and they answer questions no blind test can.

---

## 0. What each lane needs (so you know what's optional)

| Lane | Needs | Works while PC asleep? |
|---|---|---|
| Store questions (search, wishlist-on-sale, reviews, hltb…) | nothing, or the existing `steamApiKey`/`steamId64` | **yes** (cloud) |
| Big Picture nav / quit / collections | the PC deployed + two new tasks registered | no (needs a session) |
| Install-by-voice / download status | one-time account-session enrollment | no (PC must be online in Steam) |

The store lane is pure upside and depends on nothing new. Nav/quit/collections
need the PC-side deploy in Stage 1. Install needs Stage 5. You can stop after any
stage and the rest of the system is untouched — the chord lane never changes.

---

## 1. Deploy & register

**K15 (the clone on the Desktop):**

```
git pull
```

Then add the two new voice knobs to `k15\config.json` (they are optional and
default sensibly, but the live rig's `config.json` predates them — copy the
blocks from `config.example.json`):

- `"steamDataTools": true` — the store-question lane (a kill switch; leave true).
- the `"navTargets": { … }` block — the spoken names for `nav` ("downloads",
  "the store", "my library", …). Without it the `nav` *grammar* simply never
  fires (the assistant's `nav` tool still works); with it, "show downloads" is a
  one-word Tier-1 command.

Reload both lanes:

```
.\Start-K15.bat
```

The voice supervisor rebuilds its venv from `requirements.txt` on the next agent
launch, which installs the one new pin (`howlongtobeatpy`, for the how-long-to-
beat facet). It is optional and fail-soft: if the install is skipped, only that
one facet goes quiet.

**Gaming PC (from a checkout on the PC):**

```
powershell -NoProfile -ExecutionPolicy Bypass -File gaming-pc\Deploy.ps1
```

This ships the new `Nav-BigPicture.ps1` and `Stop-Game.ps1` as part of the set.
Register their two tasks once (interactive session — the guide §8.4 now lists
these):

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Nav-BigPicture.ps1'
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Nav' -Action $a -Settings $s

$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Stop-Game.ps1'
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'StopGame' -Action $a -Settings $s
```

**Confirm the chain both ways:**

- PC: `powershell -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Doctor.ps1` → `0 fail` (the `task Nav` and `task StopGame` lines should be PASS).
- K15: `python doctor.py` → `0 fail`. The new `steam session` line is silent until you enrol in Stage 5 — that is correct, the lane is optional.

---

## 2. Phase-0 gates (the questions only the rig answers)

Four quick checks. Each decides a branch or confirms a shape a keyless checkout
can't see. Run them before trusting the features built on them.

**2a — does `+app_stop` quit a game?** *(decides which path `stop` uses)*
With a game running, on the PC:

```powershell
$steam = Join-Path ((Get-ItemProperty 'HKCU:\Software\Valve\Steam').SteamPath -replace '/','\') 'steam.exe'
& $steam -ifrunning +app_stop <appid>
(Get-ItemProperty 'HKCU:\Software\Valve\Steam').RunningAppID   # watch it drop
```

If `RunningAppID` drops to 0, Steam's own graceful stop works and `Stop-Game`
will use it. If
not, `Stop-Game` falls back to a window-close and, last, a tree-kill — it
**self-adapts either way**, so this test only tells you which path to expect in
the `game_stopped method=…` event, not whether quitting works.

**2b — does `steam://` navigate *inside* Big Picture?** *(the whole nav feature)*
With a session up (Big Picture on the TV), from the PC:

```
start steam://open/downloads
start steam://open/library/details/<an-installed-appid>
start steam://store/<appid>
```

Each should move the TV's Big Picture UI, **not** pop a desktop window. Per the
client's per-UI-mode routing a wrong URL is a silent no-op, so the failure mode
is "nothing happens," not "a window breaks out." Then the harder one: with a
game running fullscreen, fire `start steam://open/downloads` — does it pull Big
Picture over the game, or navigate silently behind it? That answer decides
whether nav is safe to offer mid-game (today the K15 gates nav on READY, not on
"no game running", so you are the check).

**2c — do collections read correctly?** *(the "show my roguelikes" data)*

```
ssh gamepc collections
```

Expect `[{"name":"…","id":"uc-…"}, …]` for your real Big Picture collections.
The cloud-storage file format is community-reverse-engineered, so if this is
empty or wrong, capture `…\userdata\<id>\config\cloudstorage\cloud-storage-namespace-1.json`
and the parse in `Dispatch.ps1`'s `collections` verb needs a tweak — everything
else still works; only "show my <collection>" is affected.

**2d — is the PC reachable over ClientComm?** *(proves Stage 5 before you enrol)*
Log into `store.steampowered.com` in a browser, open
`store.steampowered.com/pointssummary/ajaxgetasyncconfig`, copy the
`webapi_token`, then:

```
curl "https://api.steampowered.com/IClientCommService/GetAllClientLogonInfo/v1/?access_token=<token>"
```

If the gaming PC shows up in `sessions` (with its `machine_name`), the whole
install-by-voice path is confirmed end to end before any enrolment. (The token
is ~24h and browser-scoped; it's just a smoke test.)

---

## 3. Store questions (safe, PC can be asleep)

**Fetchers, no voice** — from `k15\` (the venv lives in `voice\`):

```
voice\.venv\Scripts\python library.py probe deals
voice\.venv\Scripts\python library.py probe search co-op roguelike
voice\.venv\Scripts\python library.py probe reviews 1145360
voice\.venv\Scripts\python library.py probe hltb hades
voice\.venv\Scripts\python library.py probe trending
voice\.venv\Scripts\python library.py probe recent
```

Each prints JSON. `deals` should list specials and (if `steamId64` is set)
wishlist-on-sale; `search` should return real titles with prices; `hltb` should
return hours once `howlongtobeatpy` is installed (it fail-softs to `null`
otherwise). `probe deals` also writes `state\deals.json`, which the background
worker reads for grounded prices.

**Tool selection** — confirm the model reaches for these and doesn't over-
escalate (real model, small cost):

```
voice\.venv\Scripts\python voice\bench\probe_tool_select.py
```

Expect `6/6 probes correct`. Re-run after any change to a tool description or
`RULES`.

**Spoken** (agent running, PC may be asleep):

- "hey jarvis, anything on my wishlist on sale?" → names a few, cheapest/biggest
  discount first, in one breath.
- "find me a co-op roguelike under twenty bucks" → a shortlist with prices,
  **not** a "I'll go research that." Then escalate: "which of those is actually
  the best?" → *that* becomes a background task (the worker), and it comes back
  spoken later.
- "how long does it take to beat Hades?" → hours.
- "what are people saying about <a game or its DLC>?" → a review-score summary
  and the gist.

In the logs (`grafana-logs` skill or Langfuse) the first four should be single-
turn tool calls; only the "which is best" should show a `background_task`.

---

## 4. Nav, quit, collections (at the TV, session up)

Start a session first ("game time" / the chord). Then:

**Navigation:**

- "show downloads" → Big Picture's download queue.
- "show me <an installed game>" → that game's library page (Play button). This
  one goes through the assistant (game-page nav needs title resolution), so a
  half-second of think time is normal.
- "open the store page for <game>" → its store page inside Big Picture.
- "show my <collection name>" → that collection's grid. (Needs Stage 2c to have
  returned your collections; the agent syncs them each session boundary.)

Each should move the TV UI with a single confirmation earcon and no desktop
window. A miss ("show my <collection that doesn't exist>") should fall through to
the assistant, not beep-fail.

**Quit — the headline.** With a game running:

- "quit the game" → the assistant **confirms first** ("Quit <game>?") — this is
  deliberate (a mis-heard quit shouldn't kill a game). Say "yes."
- Watch: the game closes, and Big Picture comes back **in focus** (the controller
  works immediately — that re-focus is the whole point of `Stop-Game`'s last
  step). Check the `game_stopped` event for `method=` (`app_stop` / `wm_close` /
  `kill`) and `cleared=true`.
- The BUSY→quit flow: with game A running, say "play <game B>". The assistant
  should say A is running and *offer to quit it* (not "use the controller"). Say
  "quit it and play B."

**Save-safety note:** `Stop-Game` tries Steam's graceful stop, then a window
close (save-and-quit for most games), and only force-kills if both are ignored —
so a game that respects a close request will save. A game that ignores both can
lose unsaved progress on the kill; that is logged as `method=kill` at warn.

---

## 5. Install-by-voice & download status (account session)

This is the one lane that needs a credential, and it is the one with the safety
rule. Enrol once, on the K15, from `k15\voice\`:

```
.venv\Scripts\python steam_session.py enroll
```

It prints a QR. **Scan it with the Steam mobile app** (Steam Guard → scan), and
approve. On approval the refresh token is written to `secrets.json`.

- The login runs under the **WebBrowser** platform on purpose — that is the one
  documented-safe choice (a MobileApp+QR login is what gets accounts flagged as
  compromised, because the mobile app is the scanner, never the target). You do
  not need to do anything for this; it is baked in. It is also exactly what the
  Steam store website's own "install on my computer" uses.
- `python doctor.py` should now show `steam session: enrolled and minting,
  refresh token good for N days`. That probe runs a **real mint** (via
  `steam_session.py token`), not just a JWT-expiry read — an unexpired token is
  not necessarily a usable one, and believing otherwise cost a whole test
  session once. Re-enrol when it warns: a password change, a "deauthorize all
  devices", a public-IP change, or ~200 days — the same 30-second scan.
- **A dead account lane costs one button press, not the feature.** `install_game`
  falls back to opening the game's page on the TV for you to press Install, so
  the couch keeps working while you get around to re-enrolling.
- Quick check, same directory: `.venv\Scripts\python steam_session.py sessions`
  lists every signed-in client — the gaming PC should be there with its
  `machine_name`. Signed in on another PC too? Set `"steamMachineName"` in
  `config.json` to the gaming PC's name from this list — it pins install and
  download-status to the right box (empty/absent = first listed).

Restart the agent so it picks up the session (`.\Start-K15.bat`). Then, with the
PC online in Steam:

- "install <a game you own but haven't installed>" → the assistant confirms, then
  queues it. Check the TV / Steam: the download should start with **no dialog and
  no controller**. The `install_queued` event carries `verified=true` once
  `GetClientAppList` confirms it actually queued (an empty 200 alone is not
  trusted).
- "how far along is the download?" → a percent. ("what's downloading" also works.)
- If the PC is asleep/offline, the honest answer is "the PC isn't online in Steam
  right now" — that is the session simply not being listed, not an error.

---

## 6. Rollback (each lane, cleanly)

Everything is additive and the chord lane is never touched, so there is no risky
teardown — but each new lane has an off switch:

| Lane | Turn it off by |
|---|---|
| Store questions | `config.json` → `"steamDataTools": false` (the tools vanish from what the model sees) |
| Install / download status | delete `steamRefreshToken` from `secrets.json` (the lane self-gates off) |
| Nav / quit grammar | remove `navTargets` from `config.json` (the Tier-1 nav phrases stop firing; the assistant tools remain) |
| Nav / quit / collections entirely | don't register (or `Unregister-ScheduledTask`) the `Nav`/`StopGame` tasks — the verbs then answer `FAILED` and dispatch reports it |

A full revert is `git revert` on the branch, `git pull` on the K15, `Deploy.ps1`
on the PC, `Start-K15.bat` — and the two new PC tasks left unregistered. Nothing
in the chord lane, the display profiles, or the one rule changes at any point.

---

## What "it works" looks like

- `doctor.py` (K15) and `Doctor.ps1` (PC) both end `0 fail`.
- Store questions answer in one breath from the couch, PC asleep.
- "show downloads" / "show my <collection>" move the TV UI; a miss falls through.
- "quit the game" closes it **and hands the controller back to Big Picture**.
- "install <game>" starts a download on the TV with no controller, and "how far
  along" reads the percent.
- The chord still launches a game by itself, exactly as before — the proof the
  overlay stayed an overlay.
