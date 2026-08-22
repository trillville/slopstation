---
name: grafana-logs
description: Read the couch system's telemetry from Grafana Cloud Loki - launches, voice sessions, errors, crashes, and liveness across both the K15 and the gaming PC. Use when asked to check the logs or telemetry, find out what happened at a given time, diagnose why a launch or voice command failed, confirm whether a lane is alive, or investigate anything that went wrong while nobody was watching. Also for questions like "is the house up", "did anything break last night", "why didn't the chord work".
---

# Reading the couch system's logs

Every event from both machines lands in Grafana Cloud Loki. Query it from the
terminal — never send the user to a browser for something answerable here.

```bash
python .claude/skills/grafana-logs/query.py '{service="k15", level="error"}' --since 24h
```

Stack `narrownuthatch2355` (US West), `https://narrownuthatch2355.grafana.net`.
A query that returns nothing may mean the pipeline is down rather than the
query being wrong: check Alloy is running on the machine in question, then
that its `config.alloy` points at the stack above.

Credentials come from `k15/secrets.json` (gitignored). **Worktrees have no
copy of it**; the script falls back to the enclosing checkout's automatically,
so a credentials error means neither checkout has them (env
`GC_LOKI_USER`/`GC_LOKI_READ_TOKEN` also works).

## The data model

Four **labels** — cheap to select on, and the only things allowed in `{...}`:

| Label | Values |
|---|---|
| `service` | `k15` (orchestrator), `gamepc` (the gaming PC) |
| `lane` | k15: `voice`, `launch`, `listener`, `library`, `steam`, `traces`, `supervisor`, `manual` — gamepc: `enter`, `exit`, `launchgame`, `nav`, `stopgame`, `wake-safety`, `office-safety`, `pc-transcript` |
| `level` | `info`, `warn`, `error` — the whole set; there is no `debug` |
| `env` | `prod`, `test` — **always filter `env="prod"`** unless investigating the blind suite |

Everything else is a **field inside the JSON line** and needs `| json |`
first: `event`, `turn`, `session`, `dur_ms`, `err`, `appid`, `score`, …

```logql
{service="k15", level="error"}            # label — fast
{service="k15"} | json | turn="9f2c1a"    # field — needs the parser
```

## Follow a `turn`

Every user intent — a chord press, a wake word — mints a 6-hex `turn` id that
travels through dispatch, `couch.py`, the SSH boundary, and the gaming PC's
scheduled task. **One query returns the whole story across both machines.**

```bash
python .claude/skills/grafana-logs/query.py '{service=~"k15|gamepc"} | json | turn="9f2c1a"' --since 24h
```

When investigating any failure: find the failing event, take its `turn`, then
run that. Do not reconstruct a timeline from timestamps.

## Recipes

```logql
{service=~"k15|gamepc", env="prod"} | json | level=~"warn|error"   # what broke
{service="k15", lane="launch", env="prod"}                          # launches
{service="k15", lane="voice", env="prod"} | json | event="gate_miss"  # grammar misses
{service="k15", lane="supervisor"} | json | event="restart"         # crash loops
{service="gamepc", lane="pc-transcript"}                            # the PC's raw narrative
{service="gamepc", lane=~"wake-safety|office-safety"}               # the failsafes - the PC's most frequent lanes
{service="k15"} | json | session="c32ec7"                           # one voice conversation
```

Liveness — a lane is dead if this returns 0 (expect ~5, one per minute):

```logql
sum(count_over_time({service="k15", lane="listener", env="prod"} | json | event="heartbeat" [5m]))
```

Time to READY, the number the whole system is judged on:

```logql
{service="k15", lane="launch", env="prod"} | json | event="host_ready"
```

## Event vocabulary

- **launch**: `launch_start` `wol_sent` `ssh_up` `tv_on` `tv_state_unknown` `enter_dispatched` `host_ready` `launch_failed` `launch_aborted` `exit_dispatched` `cancel_void_failed` `session_ended` `session_idle` `exlink_send` `exlink_nak` `enter_died` `enter_redispatched`
  - `launch_start` carries `tv` on rigs with `tvIp`: the set's RAW PowerState
    as the launch found it — `on`, `standby` (shallow), `""` (deep: hours off,
    still answering with the field drained) or `unreachable` (a sentinel; the
    emitter drops None fields, so the read's own None cannot ship). Whether
    `""` predicts a refused wake is unmeasured.
  - `tv_on` is the TV evidence (couch.py `tv_poll`) confirming the set
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
  `| json | event="exlink_send"`
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
    session build. Deepgram's ceiling is 100 keyterms (measured; 110 is a 400
    on connect) and `headroom` is what is left of it, so alert on
    `headroom < 5` rather than waiting for `keyterms_capped`.
  - `audio_device_wait` — the configured mic is not in the device table;
    `waited_s` is how long the agent has been deaf waiting for it. A rebuild
    never falls back to the system default, so this event standing still is
    the outage.
- **voice, assistant lane**: `tool_call` — one per tool the assistant ran, with
  `tool`, `ok` and truncated `args`. Nothing before 2026-08-14 has it. Also
  `tool_refused` (the boundary rejecting a call, e.g. `reason=unknown_appid`),
  `job_requested` (the background brief), `web_search` (provider-executed
  search).
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
- **background jobs**: `job_queued` `job_running` `job_done` `job_failed`
  `job_announced` `job_orphaned` — on `lane="voice"`, not a lane of their own
  (the JobStore logs through the voice agent's logger), so select them by
  `event`. `job_done` carries `cost_usd` / `turns` / `web_searches`.
- **steam**: `enrolled` `token_mint_failed` `token_transfer_failed`
  `install_queued` `install_failed` — the account session. The lane label means
  **hand-run**, like `manual` does for `exlink.py`: the agent passes its own
  logger in, so a voice-driven session files these under `lane="voice"` and
  only `python steam_session.py …` at the console lands on `lane="steam"`.
  Select on `event` when you want both.
- **gamepc**: `enter_start` `profile_applied` (carries `retried` on Enter - true means the first TV-GAMING apply missed and the retry rescued the launch) `profile_retry` `puck_claimed` `ready` (carries `focused`, `fg` = the window that actually held the foreground, and `running_appid` = a game already up at Enter, 0 if none) `enter_failed` `exit_done` `game_launched` `nav_fired` (carries the `steam://` url) `nav_failed` `game_stopped` (carries `method` — `app_stop` / `wm_close` / `kill` / `already-gone`, i.e. WHICH escalation rung actually quit it, plus `cleared`) `game_stop_failed`

- **dispatch** (gaming PC, `logs\pc-dispatch-*.jsonl` - its own file, because
  Dispatch runs elevated and the task scripts do not): `verb` - every mutating
  verb (`enter` `exit` `launch` `nav` `stop`) with `verb`, `answer` and the
  `turn` it carried; a `DENIED` command lands at `level="warn"` with
  `answer=DENIED` and the first 60 chars as `cmd`. The read-only polls stay
  silent.
- Newer names on existing lanes: launch `config_invalid` (a config doctor would
  FAIL, refused before the lock and before `power_on`), `enter_refused` (a
  non-OK answer to `enter`, once per distinct answer - today the K15 retried
  in silence), `reconcile_cleared reason=unreachable` (the PC did not answer
  at boot; `dead_session` now means it answered NOTREADY); voice `tool_error`
  (a tool impl raised; pairs with the `tool_call ok=false` for the same call);
  gamepc `profile_applied` / `profile_apply_failed` on `lane="office-safety"`
  (a logon that had to restore OFFICE) and `wake_cleanup` on
  `lane="wake-safety"`.

The frozen list - every name, its field keys and its lane - is
`k15/voice/tests/test_event_names.py`; a rename is a deliberate edit there.

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

```logql
{service="gamepc", lane="pc-transcript"} |= "DisplayMagician"
```

## How to answer well

1. **Start narrow, widen if empty.** `--since 6h` is the default.
2. **Lead with the finding, not the query**, and quote only the few lines that
   prove it.
3. **`dur_ms` is measured from the chord or wake word**, not from when that
   step started — elapsed-since-intent, so it always increases down a launch.
4. **A quiet result can be the answer.** No `heartbeat` means a dead lane; no
   `launch_failed` means launches are fine. Say so rather than "nothing found".
5. **`couch.log` on the K15 is the offline mirror.** If Loki has a gap, the
   local JSONL (`k15/logs/k15-*.jsonl`) is the source of truth — lines read
   while the shipper was failing are dropped, not queued.

## When a query returns nothing

Check, in order: is `env="prod"` filtering out what you want; is the time
range too narrow; is the field name right (`| json |` present?); has the
gaming PC's shipper been installed (E4). A 401 means the token lacks
`logs:read` — the shipper's write token gives exactly that error.
