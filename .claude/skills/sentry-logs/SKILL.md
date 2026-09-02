---
name: sentry-logs
description: Read the couch system's telemetry from Sentry - launches, voice sessions, errors, crashes, and liveness across both the K15 and the gaming PC. Use when asked to check the logs or telemetry, find out what happened at a given time, diagnose why a launch or voice command failed, confirm whether a lane is alive, or investigate anything that went wrong while nobody was watching. Also for questions like "is the house up", "did anything break last night", "why didn't the chord work". This is the CURRENT log store; the grafana-logs skill is the one being retired.
---

# Reading the couch system's logs

Every event from both machines lands in one Sentry project. Query it from the
terminal — never send the user to a browser for something answerable here.

```bash
python .claude/skills/sentry-logs/query.py --service k15 --level error --since 24h
```

Drive it with flags, not a raw query string: PowerShell strips the double
quotes out of a query on its way to a native process. The script composes the
search from `--service --lane --level --event --turn --session --contains
--env --since --limit` (`--env` defaults to `prod`, `--since` to `6h`), prints
the query it built, and takes `--query` for anything the flags cannot express.

**During the migration** both stores are live. Loki (the `grafana-logs` skill)
is the fallback while Sentry soaks; prefer this one, and cross-check there if
a result looks wrong rather than concluding the system was quiet.

A query that returns nothing may mean the pipeline is down rather than the
query being wrong: check the `otelcol-contrib` service is running on the
machine in question (`python doctor.py` has a row for it), then that its
config points at the project in `config.json`. That collector is a separate
process from Alloy, which still ships the same files to Loki until the
migration finishes - one being down says nothing about the other.

Credentials come from `k15/secrets.json` and `k15/config.json` (both
gitignored). **Worktrees have no copy of either**; the script falls back to the
enclosing checkout automatically, so a credentials error means neither
checkout has them (env `SENTRY_READ_TOKEN`/`SENTRY_ORG`/`SENTRY_PROJECT` also
works).

## The data model

Everything the emitter wrote is a searchable **attribute** — there is no
label/field split here, and no `| json |`. What was a Loki label is now just an
attribute that happens to have few values:

| Attribute | Values |
|---|---|
| `service` | `k15` (orchestrator), `gamepc` (the gaming PC) |
| `lane` | k15: `voice`, `launch`, `listener`, `library`, `steam`, `traces`, `supervisor`, `manual` — gamepc: `enter`, `exit`, `launchgame`, `nav`, `stopgame`, `wake-safety`, `office-safety`, `dispatch`, `pc-transcript` |
| `severity` | `info`, `warn`, `error` — the whole set; there is no `debug`. This is the `level` field, mapped to Sentry's own severity by the shipper |
| `env` | `prod`, `test` — **always filter `env:prod`** unless investigating the blind suite |

`event`, `turn`, `session`, `dur_ms`, `err`, `appid`, `score` and the rest are
attributes too, and cost nothing extra to filter on. The log body is the
original JSONL line, which is what the script renders.

```text
env:prod service:k15 severity:error       # attributes, all equal
env:prod turn:9f2c1a                      # no parser step needed
```

## Follow a `turn`

Every user intent — a chord press, a wake word — mints a 6-hex `turn` id that
travels through dispatch, `couch.py`, the SSH boundary, and the gaming PC's
scheduled task. **One query returns the whole story across both machines.**

```bash
python .claude/skills/sentry-logs/query.py --turn 9f2c1a --since 24h
```

`--turn` and `--session` span both machines by themselves — the script only
pins `service` when you pass `--service`.

When investigating any failure: find the failing event, take its `turn`, then
run that. Do not reconstruct a timeline from timestamps.

## Recipes

Each of these has a flag form; the search string is what the script builds.

```text
env:prod severity:[warn,error]                    # what broke
env:prod service:k15 lane:launch                  # launches
env:prod service:k15 lane:voice event:gate_miss   # grammar misses
env:prod lane:supervisor event:restart            # crash loops
env:prod service:gamepc lane:pc-transcript        # the PC's raw narrative
env:prod service:gamepc lane:[wake-safety,office-safety]   # the failsafes
env:prod session:c32ec7                           # one voice conversation
```

Time to READY, the number the whole system is judged on:

```bash
python .claude/skills/sentry-logs/query.py --lane launch --event host_ready --since 7d
```

`dur_ms` is a numeric attribute here, so the aggregate the Loki version could
not do is a chart in Sentry: average and p95 of `dur_ms` on `event:host_ready`.

## Liveness is not a log query any more

**Do not answer "is the lane alive" from the logs.** Each lane checks in to a
Sentry cron monitor every 60 seconds (`k15/checkin.py`), and a missed check-in
opens an issue on its own — that signal does not ride the log pipeline, so it
survives a dead shipper. Read the monitors, not `event:heartbeat`.

The `heartbeat` events still ship and are still worth counting for one thing:
they prove the SHIPPER is alive. No heartbeats plus a healthy cron monitor
means Alloy died, not the lane.

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
    `tv_state_unknown` (WARN) is the evidence standing down after
    consecutive unreadable answers — the launch proceeds on the legacy
    blind path. While the set answers not-on it re-pokes `power_on` every
    ~6 s (so a set that takes the second frame lights inside Enter's own
    retry and no death is recorded), and after `enter_died` the rescue
    waits for the set's "on" before spending its one redispatch — a set
    that keeps answering not-on fails with `launch_failed` err
    `TV never reported on` (~60-90 s all told).
  - `enter_died` means the PC's Enter task exited WITHOUT writing the marker;
    the launch was lost at that moment. `enter_redispatched` is the rescue
    that follows: another `power_on`, another Enter. A turn carrying both and
    then `host_ready` is one the TV refused to wake for on the first ask, so
    **`enter_died` counts the TV**, not the PC — rare on rigs with `tvIp`
    (the TV story moves to `tv_on`/`tv_state_unknown`), and on older deploys
    the closest thing to a TV-power metric there is.
  - `launch_aborted` is a deliberate stop, not a failure: no `last_error`,
    and the Puck is not buzzed. `err=KeyboardInterrupt` is Ctrl-C in the
    launch console; `err=Cancelled` is a voice "end the session" against an
    in-flight launch (the `state/cancel` marker), and `cancelled_by` carries
    the cancelling utterance's turn.
- **manual**: `exlink_send` `exlink_nak` `tvremote_send`
  `tvremote_fail` — the same events from a hand-run `python exlink.py <cmd>`,
  kept off the launch lane so operator probing does not skew launch metrics.
  Drop the lane to see every frame whoever sent it:
  `event:exlink_send`
- **voice**: `wake` `stt_final` `gate_match` `gate_miss` `title_resolved` `title_miss` `dispatch` `session_open` `session_stop_requested` `session_close` `session_crashed` `pipeline_error` `heartbeat`
  - Room ducking (TvDucker): `tv_ducked` / `tv_unducked` (both carry `steps` =
    verified movement vs `asked`, plus `vol` and `ok`; `tv_unducked` may carry
    `reason=user_adjusted|no_readback`), `tv_duck_skipped` (the on-gate:
    `state=standby|unknown` or `reason=no_readback`), `tv_duck_failed` (a key
    burst or the whole op raised), `tv_duck_deficit` (WARN: steps still owed —
    the next session's close retries them). Substring gotcha: `tv_unducked`
    does not contain "tv_duck", so match `tv_` or the event names, never
    `|= "tv_duck"`.
  - `gate_match`/`gate_miss`/`stt_final` carry `confidence` (mean per-word, from
    Flux) — bad transcript vs bad phrasing. Absent on turns where Flux sent no
    per-word data.
  - `stt_vocabulary` `keyterms_capped` — what the STT was told to expect at
    session build. Deepgram's ceiling is 100 keyterms (documented, and measured;
    110 is a 400 on connect) and `headroom` is what is left of it, so alert on
    `headroom < 5` rather than waiting for `keyterms_capped`.
  - `audio_device_wait` — the configured mic is not in the device table;
    `waited_s` is how long the agent has been deaf waiting for it. A rebuild
    never falls back to the system default, so this event standing still is
    the outage.
- **voice, assistant lane**: `tool_call` — one per tool the assistant ran, with
  `tool`, `ok` and truncated `args`. Nothing before 2026-08-14 has it. Also
  `tool_refused` (the boundary rejecting a call, e.g. `reason=unknown_appid`),
  and `web_search` (provider-executed search).
- **voice, couch verbs**: `nav_dispatched` `quit_dispatched` — both carry the
  host's `answer`, and `FAILED:1` on either means the PC-side scheduled task
  is not registered, not that the verb is broken. Plus `collection_resolved`
  `collection_miss` `install_queued` `install_error` `download_status_error`
- **library**: `sync_done` / `sync_skipped` (both carry `layer` — installed,
  collections, owned, meta, deals), `deals_synced` `meta_fetched`
  `store_fetch_failed` `hltb_failed`
- **listener**: `chord` `chord_busy` `chord_partial` `puck_present` `puck_vanished` `puck_standoff` `armed` `heartbeat`
  - `chord_partial` carries the button byte actually seen (`btn`) against the
    one the chord needs (`want`), rate-limited to one per 10 s. When the chord
    did nothing and the Puck never buzzed: partials present means buttons ARE
    arriving and the press was wrong or too short; `armed` with no partials at
    all is a claim/firmware problem, not a hold problem.
- **supervisor**: `start` `restart` `lane_started` `lane_reloaded` `deps_installed`
- **operations**: `operation_created` `operation_observed`
  `operation_announced` `operation_cancel_refused` — durable observations of
  externally-owned work on `lane="voice"`. `operation_observed` carries the
  previous/current state, structured progress, and detail. `UNKNOWN` is an
  observation gap, not a failure; terminal delivery is `operation_announced`.
- **steam**: `enrolled` `token_mint_failed` `token_transfer_failed`
  `install_queued` `install_failed` — the account session. The lane label means
  **hand-run**, like `manual` does for `exlink.py`: the agent passes its own
  logger in, so a voice-driven session files these under `lane="voice"` and
  only `python tools/steam_session.py …` at the console lands on `lane="steam"`.
  Select on `event` when you want both.
- **gamepc**: `enter_start` `profile_applied` (carries `retried` on Enter - true means the first TV-GAMING apply missed and the retry rescued the launch) `profile_retry` `puck_claimed` `ready` (carries `focused`, `fg` = the window that actually held the foreground, and `running_appid` = a game already up at Enter, 0 if none) `enter_failed` `exit_done` `game_launched` `nav_fired` (carries the `steam://` url) `nav_failed` `game_stopped` (carries `method` — `app_stop` / `wm_close` / `kill` / `already-gone`, i.e. WHICH escalation rung actually quit it, plus `cleared`) `game_stop_failed`

- **dispatch** (gaming PC, `logs\pc-dispatch-*.jsonl` - its own file, because
  Dispatch runs elevated and the task scripts do not): `verb` - every mutating
  verb (`enter` `exit` `launch` `nav` `stop`) with `verb`, `answer` and the
  `turn` it carried; a `DENIED` command lands at `level="warn"` with
  `answer=DENIED` and the first 60 chars as `cmd`. The read-only polls stay
  silent.
- launch, also: `config_invalid` (a config doctor would FAIL - refused before
  the lock and before `power_on`; carries `missing` or `err`), `enter_refused`
  (a non-OK answer to `enter` - `NOTASK:Enter`, `FAILED:<code>` - once per
  distinct answer), `reconcile_cleared` with `reason=dead_session` (the PC
  answered NOTREADY) or `reason=unreachable` (it never answered at boot).
- voice, also: `tool_error` (a tool impl raised; pairs with the `tool_call
  ok=false` for the same call).
- gamepc, also: `profile_applied` / `profile_apply_failed` on
  `lane="office-safety"` (a logon that had to restore OFFICE), `wake_cleanup`
  on `lane="wake-safety"`.

The frozen list - every name, its field keys and its lane - is
`k15/agent/tests/test_event_names.py`; a rename is a deliberate edit there.

Event names are a closed vocabulary and never contain variable data — an
appid or a score is always a field.

Two fields that mislead if read at face value:

- **`ack` on `exlink_send` is not confirmation.** It means the TV's serial
  receiver accepted the frame; Ex-Link here is send-only and nothing reads TV
  power back. A `power_on` can ack and leave the set dark (2026-08-13).
- **`primary_height` on `enter_failed`** separates the two failure shapes: the
  desk's own height means the TV never came up, while anything else (or `-1`)
  means the failed apply detached the desktop and left no active display.

When a launch fails on the profile, the gaming PC copies the interesting lines
of DisplayMagician's own log next to the transcript, so it ships under
`lane="pc-transcript"`:

```bash
python .claude/skills/sentry-logs/query.py --service gamepc --lane pc-transcript --contains DisplayMagician
```

## How to answer well

1. **Start narrow, widen if empty.** `--since 6h` is the default.
2. **Lead with the finding, not the query**, and quote only the few lines that
   prove it.
3. **`dur_ms` is measured from the chord or wake word**, not from when that
   step started — elapsed-since-intent, so it always increases down a launch.
4. **A quiet result can be the answer.** No `heartbeat` means a dead lane; no
   `launch_failed` means launches are fine. Say so rather than "nothing found".
5. **`couch.log` on the K15 is the offline mirror.** If Sentry has a gap, the
   local JSONL (`k15/logs/k15-*.jsonl`) is the source of truth — lines read
   while the shipper was failing are dropped, not queued.

## When a query returns nothing

Check, in order: is `env:prod` filtering out what you want; is the time range
too narrow; is the attribute spelled the way `events.py` wrote it; has that
machine's otelcol-contrib service been installed and pointed at Sentry. A 401 means the token is
missing or lacks `org:read` — the DSN public key in `config.json` is an
ingest key and cannot query. `--json` shows exactly what the API returned.
