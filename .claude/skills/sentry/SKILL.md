---
name: sentry
description: Read the couch system's telemetry from Sentry - logs, crashes, voice traces and lane liveness across the K15 and the gaming PC. Use when asked to check the logs or telemetry, find out what happened at a given time, diagnose why a launch or voice command failed, confirm whether a lane is alive, inspect an assistant conversation or its token use, or investigate anything that went wrong while nobody was watching. Also for questions like "is the house up", "did anything break last night", "why didn't the chord work".
---

# Reading the couch system's telemetry

Everything lands in ONE Sentry project, `slopstation`: both machines, all
lanes, plus the voice pipeline's traces. That is deliberate - a `turn` is one
query, not two.

Read it with the **Sentry MCP tools** (server `sentry`, configured in
`.mcp.json`). Never send the user to a browser for something answerable here.
Four surfaces, and picking the right one is most of the job:

| Surface | Holds | Reach for it when |
|---|---|---|
| **Logs** | every `events.py` / `Write-CgEvent` line from both machines | almost always - this is the workhorse |
| **Issues** | unhandled crashes with stack traces | "it died and I want to know where" |
| **Traces** | one voice session: stt → llm → tts, with timings | "why was that turn slow" |
| **Agents / Conversations** | LLM calls, tools, tokens, prompts | "what did the assistant actually do" |

Retention: logs and spans 30 days, errors 90. The local JSONL
(`k15/logs/k15-*.jsonl`) keeps 14 days and is the offline source of truth.

## Follow a `turn`

Every user intent — a chord press, a wake word — mints a 6-hex `turn` id that
travels through dispatch, `couch.py`, the SSH boundary, and the gaming PC's
scheduled task. **One query returns the whole story across both machines.**

```
turn:9f2c1a
```

When investigating any failure: find the failing event, take its `turn`, then
run that. Do not reconstruct a timeline from timestamps.

A voice session has a second id, `session`, shared by every turn in the
conversation — and it is also the Conversation id in agent monitoring, so the
same value moves between the log list and the assistant's transcript.

## The data model

A log record's **message is the event name**; every field `events.emit` was
given is an attribute beside it.

| Attribute | Values |
|---|---|
| `service` | `k15` (orchestrator), `gamepc` (the gaming PC) |
| `lane` | k15: `voice` `launch` `listener` `library` `steam` `traces` `supervisor` `manual` `deploy` — gamepc: `enter` `exit` `launchgame` `nav` `stopgame` `wake-safety` `office-safety` `dispatch` `pc-transcript` |
| `severity` | `info` `warn` `error` — the whole set; there is no `debug` |
| `env` | `prod`, `test` — **always filter `env:prod`** unless investigating the blind suite |
| `event` | the closed vocabulary below |

Everything else is per-event: `turn` `session` `dur_ms` `err` `appid` `score` …

```
env:prod severity:error                       # what broke
env:prod lane:launch event:launch_failed      # failed launches
env:prod event:[gate_miss,title_miss]         # the assistant not understanding
```

## Recipes

```
env:prod severity:[warn,error]                       what broke, both machines
env:prod lane:launch                                 launches
env:prod lane:voice event:gate_miss                  grammar misses
env:prod lane:supervisor event:restart               crash loops
service:gamepc lane:pc-transcript                    the PC's raw narrative
service:gamepc lane:[wake-safety,office-safety]      the failsafes
env:prod event:host_ready                            time to READY - the number the system is judged on
env:prod lane:dispatch answer:DENIED                 verbs the PC refused
```

**Liveness is not a log query.** Two cron monitors, `k15-listener` and
`k15-voice`, check in every minute; a missed check-in pages on its own. Read
them together with the heartbeat count:

- a cron red → that lane is down
- crons green but `event:heartbeat` empty → the **collector** is down, not the lanes
- both quiet → the K15 is off

## Event vocabulary

- **launch**: `launch_start` `wol_sent` `ssh_up` `tv_on` `tv_state_unknown` `enter_dispatched` `host_ready` `launch_failed` `launch_aborted` `exit_dispatched` `cancel_void_failed` `session_ended` `session_idle` `exlink_send` `exlink_nak` `enter_died` `enter_redispatched`
  - `launch_start` carries `tv` on rigs with `tvIp`: the set's RAW PowerState
    as the launch found it — `on`, `standby` (shallow), `""` (deep: hours off,
    still answering with the field drained) or `unreachable` (a sentinel; the
    emitter drops None fields, so the read's own None cannot ship). Whether
    `""` predicts a refused wake is unmeasured.
  - `tv_on` is the TV evidence (couch.py `TvEvidence.poll`) confirming the set
    REPORTS on. It rides the READY wait — Enter is never gated on it, so a
    healthy launch pays nothing. Its `dur_ms` is elapsed-since-intent, and
    polling starts only after `ssh_up`, so cold boots censor it (the PC
    dominates); read frame-to-lit from warm-PC launches only.
    `tv_state_unknown` (warn) is the evidence standing down after consecutive
    unreadable answers — the launch proceeds on the legacy blind path. While
    the set answers not-on it re-pokes `power_on` every ~6 s (so a set that
    takes the second frame lights inside Enter's own retry and no death is
    recorded), and after `enter_died` the rescue waits for the set's "on"
    before spending its one redispatch — a set that keeps answering not-on
    fails with `launch_failed` err `TV never reported on` (~60-90 s all told).
  - `enter_died` means the PC's Enter task exited WITHOUT writing the marker;
    the launch was lost at that moment. `enter_redispatched` is the rescue
    that follows: another `power_on`, another Enter. A turn carrying both and
    then `host_ready` is one the TV refused to wake for on the first ask, so
    **`enter_died` counts the TV**, not the PC — rare on rigs with `tvIp`
    (the TV story moves to `tv_on`/`tv_state_unknown`), and on older deploys
    the closest thing to a TV-power metric there is.
  - `launch_aborted` is a deliberate stop, not a failure: no `last_error`, and
    the Puck is not buzzed. `err=KeyboardInterrupt` is Ctrl-C in the launch
    console; `err=Cancelled` is a voice "end the session" against an in-flight
    launch (the `state/cancel` marker), and `cancelled_by` carries the
    cancelling utterance's turn.
- **manual**: `exlink_send` `exlink_nak` `tvremote_send` `tvremote_fail` — the
  same events from a hand-run `python exlink.py <cmd>`, kept off the launch
  lane so operator probing does not skew launch metrics. Drop the lane to see
  every frame whoever sent it.
- **voice**: `wake` `stt_final` `gate_match` `gate_miss` `title_resolved` `title_miss` `dispatch` `session_open` `session_stop_requested` `session_close` `session_crashed` `pipeline_error` `heartbeat` `checkin` `checkin_failed`
  - Room ducking (TvDucker): `tv_ducked` / `tv_unducked` (both carry `steps` =
    verified movement vs `asked`, plus `vol` and `ok`; `tv_unducked` may carry
    `reason=user_adjusted|no_readback`), `tv_duck_skipped` (the on-gate:
    `state=standby|unknown` or `reason=no_readback`), `tv_duck_failed` (a key
    burst or the whole op raised), `tv_duck_deficit` (warn: steps still owed —
    the next session's close retries them). Substring gotcha: `tv_unducked`
    does not contain "tv_duck", so match the event names, never a `tv_duck`
    substring.
  - `gate_match` / `gate_miss` / `stt_final` carry `confidence` (mean per-word,
    from Flux) — bad transcript vs bad phrasing. Absent on turns where Flux
    sent no per-word data.
  - `stt_vocabulary` `keyterms_capped` — what the STT was told to expect at
    session build. Deepgram's ceiling is 100 keyterms (documented, and
    measured; 110 is a 400 on connect) and `headroom` is what is left of it,
    so alert on `headroom < 5` rather than waiting for `keyterms_capped`.
  - `audio_device_wait` — the configured mic is not in the device table;
    `waited_s` is how long the agent has been deaf waiting for it. A rebuild
    never falls back to the system default, so this event standing still is
    the outage.
- **voice, assistant lane**: `tool_call` — one per tool the assistant ran, with
  `tool`, `ok` and truncated `args`. Nothing before 2026-08-14 has it. Also
  `tool_refused` (the boundary rejecting a call, e.g. `reason=unknown_appid`),
  and `web_search` (provider-executed search). Every one of these also exists
  as a span under agent monitoring, where the arguments are untruncated.
- **voice, couch verbs**: `nav_dispatched` `quit_dispatched` — both carry the
  host's `answer`, and `FAILED:1` on either means the PC-side scheduled task
  is not registered, not that the verb is broken. Plus `collection_resolved`
  `collection_miss` `install_queued` `install_error` `download_status_error`
- **library**: `sync_done` / `sync_skipped` (both carry `layer` — installed,
  collections, owned, meta, deals), `deals_synced` `meta_fetched`
  `store_fetch_failed` `hltb_failed`
- **listener**: `chord` `chord_busy` `chord_partial` `puck_present`
  `puck_vanished` `puck_standoff` `armed` `heartbeat` `checkin`
  `checkin_failed`
  - `chord_partial` carries the button byte actually seen (`btn`) against the
    one the chord needs (`want`), rate-limited to one per 10 s. When the chord
    did nothing and the Puck never buzzed: partials present means buttons ARE
    arriving and the press was wrong or too short; `armed` with no partials at
    all is a claim/firmware problem, not a hold problem.
- **supervisor**: `start` `restart` `lane_started` `lane_reloaded`
  `deps_installed`
- **telemetry**: `checkin` / `checkin_failed` (carry `monitor`) — the cron
  check-in for that lane. A `checkin_failed` on `voice` alone almost always
  means the second cron monitor was never registered: every Sentry plan
  includes one, and the second needs a pay-as-you-go budget.
- **operations**: `operation_created` `operation_observed`
  `operation_announced` `operation_cancel_refused` — durable observations of
  externally-owned work on `lane:voice`. `operation_observed` carries the
  previous/current state, structured progress, and detail. `UNKNOWN` is an
  observation gap, not a failure; terminal delivery is `operation_announced`.
- **steam**: `enrolled` `token_mint_failed` `token_transfer_failed`
  `install_queued` `install_failed` — the account session. The lane means
  **hand-run**, like `manual` does for `exlink.py`: the agent passes its own
  logger in, so a voice-driven session files these under `lane:voice` and only
  `python tools/steam_session.py …` at the console lands on `lane:steam`.
  Select on `event` when you want both.
- **gamepc**: `enter_start` `profile_applied` (carries `retried` on Enter —
  true means the first TV-GAMING apply missed and the retry rescued the
  launch) `profile_retry` `puck_claimed` `ready` (carries `focused`, `fg` = the
  window that actually held the foreground, and `running_appid` = a game
  already up at Enter, 0 if none) `enter_failed` `exit_done` `game_launched`
  `nav_fired` (carries the `steam://` url) `nav_failed` `game_stopped`
  (carries `method` — `app_stop` / `wm_close` / `kill` / `already-gone`, i.e.
  WHICH escalation rung actually quit it, plus `cleared`) `game_stop_failed`
- **dispatch** (gaming PC, its own file because Dispatch runs elevated and the
  task scripts do not): `verb` — every mutating verb (`enter` `exit` `launch`
  `nav` `stop`) with `verb`, `answer` and the `turn` it carried; a `DENIED`
  command lands at `severity:warn` with `answer=DENIED` and the first 60 chars
  as `cmd`. The read-only polls stay silent.
- **launch, also**: `config_invalid` (a config doctor would FAIL — refused
  before the lock and before `power_on`; carries `missing` or `err`),
  `enter_refused` (a non-OK answer to `enter` — `NOTASK:Enter`,
  `FAILED:<code>` — once per distinct answer), `reconcile_cleared` with
  `reason=dead_session` (the PC answered NOTREADY) or `reason=unreachable` (it
  never answered at boot).
- **voice, also**: `tool_error` (a tool impl raised; pairs with the `tool_call
  ok=false` for the same call).
- **gamepc, also**: `profile_applied` / `profile_apply_failed` on
  `lane:office-safety` (a logon that had to restore OFFICE), `wake_cleanup` on
  `lane:wake-safety`.

The frozen list — every name, its field keys and its lane — is
`k15/agent/tests/test_event_names.py`; a rename is a deliberate edit there.

Event names are a closed vocabulary and never contain variable data — an appid
or a score is always a field.

Two fields that mislead if read at face value:

- **`ack` on `exlink_send` is not confirmation.** It means the TV's serial
  receiver accepted the frame; Ex-Link here is send-only and nothing reads TV
  power back. A `power_on` can ack and leave the set dark (2026-08-13).
- **`primary_height` on `enter_failed`** separates the two failure shapes: the
  desk's own height means the TV never came up, while anything else (or `-1`)
  means the failed apply detached the desktop and left no active display.

When a launch fails on the profile, the gaming PC copies the interesting lines
of DisplayMagician's own log next to the transcript, so it ships under
`lane:pc-transcript` — search that lane for `DisplayMagician`.

## Traces and agent monitoring

One voice session is one trace and one Conversation, both keyed on the
`session` id, so a log line's `session` is the handle for all three.

- The **trace** is pipecat's tree: conversation → turn → stt / llm / tts, with
  time to first byte per stage. Read it when a turn was slow and the logs do
  not say which stage.
- The **agent** view holds the LLM calls: `chat <model>` spans with prompts,
  completions, tool definitions and token counts, plus `execute_tool` spans
  beside them. Read it when the assistant did something odd — the log's
  `tool_call` args are truncated, these are not.
- Both lanes appear: the voice pipeline, and the text/MCP interface, which
  drives the SDKs directly and shows up as `invoke_agent assistant`.

Spans carry transcripts and completions verbatim. Treat them as private.

## How to answer well

1. **Start narrow, widen if empty.** Default to the last few hours.
2. **Lead with the finding, not the query**, and quote only the few lines that
   prove it.
3. **`dur_ms` is measured from the chord or wake word**, not from when that
   step started — elapsed-since-intent, so it always increases down a launch.
4. **A quiet result can be the answer.** No `heartbeat` means a dead shipper or
   a dead lane (the crons tell you which); no `launch_failed` means launches
   are fine. Say so rather than "nothing found".
5. **`couch.log` and the JSONL on the K15 are the offline mirror.** If Sentry
   has a gap, `k15/logs/k15-*.jsonl` is the source of truth — but the
   collector persists its read offsets, so an outage backfills rather than
   skipping.

## When a query returns nothing

Check, in order: is `env:prod` filtering out what you want; is the time range
too narrow; is the attribute name right; is the collector running on the
machine in question (`python doctor.py` on the K15 has a row for it). A gap on
one machine only is that machine's collector — the two ship independently.
