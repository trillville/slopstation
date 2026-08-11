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

## 2. Code + venv on the K15

Get the `k15/` folder onto the K15 from the `voice` branch — **exclude
`.venv`** (a venv from the dev box is the wrong Python and isn't portable):

- `git clone`/`pull` the repo on the K15 and `git checkout voice`, then work in
  its `k15/` dir; **or** copy the gaming PC's `C:\Users\tillm\projects\slopstation\k15\`
  folder over, skipping `voice\.venv\`.

Then, in the `k15\voice` folder:

```
python --version            (want 3.11+; the K15 is 3.13)
python -m venv .venv        (if python isn't on PATH: py -3.13 -m venv .venv)
.venv\Scripts\pip install -r requirements.txt
```

Create `..\secrets.json` from `..\secrets.template.json` and paste your keys.
(The chord listener, if running, is a separate process — voice runs alongside
it and can't disturb it. Fine to test with it up or down.)

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
Each should print a `030cf1` ack and move the TV. **`probe_volume` is a real
question:** if it returns bytes, the TV answers status queries (we can validate
acks and read true mute state); if silence, we stay with software-tracked mute.
**Paste what `probe_volume` returns** — it decides an open design item.

## 7. Live commands — real dispatch

Drop `--dry-run`. Now actions execute.

```
.venv\Scripts\python voice_agent.py
```

- "volume up" / "mute" / "volume 15" → the TV moves
- "switch to the playstation" → input changes; "switch to the pc" → refused
  unless a session is live (the one rule)
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

## Fastest path to "it works"

Keys (Deepgram) → §2 setup → §3 devices+spike → §4 wake → **§5 dry-run** (proves
voice end-to-end, safely). Everything after §5 is making real things happen.

## What to paste back if something's off

`--devices` output · the spike summary · wake-trial scores · a `couch.log` chunk
around the failing session · the `probe_volume` result · and the exact console
error if anything throws. The whole system logs `[voice]` lines to `couch.log`
next to `config.json`.
