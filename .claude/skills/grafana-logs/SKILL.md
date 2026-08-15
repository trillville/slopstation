---
name: grafana-logs
description: Read the couch system's telemetry from Grafana Cloud Loki - launches, voice sessions, errors, crashes, and liveness across both the K15 and the gaming PC. Use when asked to check the logs or telemetry, find out what happened at a given time, diagnose why a launch or voice command failed, confirm whether a lane is alive, or investigate anything that went wrong while nobody was watching. Also for questions like "is the house up", "did anything break last night", "why didn't the chord work".
---

# Reading the couch system's logs

Every event from both machines lands in Grafana Cloud Loki. Query it from the
terminal — never make the user open a browser to answer a question you can
answer here.

```bash
python .claude/skills/grafana-logs/query.py '{service="k15", level="error"}' --since 24h
```

Stack `narrownuthatch2355` (US West), `https://narrownuthatch2355.grafana.net`.
When a query returns nothing and you suspect the pipeline rather than the
query, [troubleshooting.md](../../../docs/troubleshooting.md) § *Telemetry
stopped arriving* is the runbook.

## The data model — read this before writing a query

Four **labels**. Selecting on these is cheap and they are the only things
allowed in `{...}`:

| Label | Values |
|---|---|
| `service` | `k15` (orchestrator), `gamepc` (the gaming PC) |
| `lane` | k15: `voice`, `launch`, `listener`, `library`, `jobs`, `supervisor`, `traces`, `manual` — gamepc: `enter`, `exit`, `launchgame`, `wake-safety`, `office-safety`, `pc-transcript` |
| `level` | `debug`, `info`, `warn`, `error` |
| `env` | `prod`, `test` — **always filter `env="prod"`** unless investigating the blind suite |

Everything else is a **field inside the JSON line** and needs `| json |`
first: `event`, `turn`, `session`, `dur_ms`, `err`, `appid`, `score`, …

```logql
{service="k15", level="error"}            # label — fast
{service="k15"} | json | turn="9f2c1a"    # field — needs the parser
```

## The one move that matters: follow a `turn`

Every user intent — a chord press, a wake word — mints a 6-hex `turn` id that
travels through dispatch, `couch.py`, the SSH boundary, and the gaming PC's
scheduled task. **One query returns the whole story across both machines.**

```bash
python .claude/skills/grafana-logs/query.py '{service=~"k15|gamepc"} | json | turn="9f2c1a"' --since 24h
```

When investigating any failure: find the failing event, take its `turn`, then
run that. Do not reconstruct a timeline by eyeballing timestamps — that is the
exact problem this system was built to remove.

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

- **launch**: `launch_start` `wol_sent` `ssh_up` `enter_dispatched` `host_ready` `launch_failed` `session_ended` `session_idle` `exlink_send` `exlink_nak`
- **manual**: `exlink_send` `exlink_nak` — the same two events from a hand-run
  `python exlink.py <cmd>`, kept off the launch lane so operator probing does
  not skew launch metrics. Drop the lane from a query to see every frame
  whoever sent it: `| json | event="exlink_send"`
- **voice**: `wake` `stt_final` `gate_match` `gate_miss` `title_resolved` `title_miss` `dispatch` `session_open` `session_stop_requested` `session_close` `session_crashed` `pipeline_error` `heartbeat`
- **voice, assistant lane**: `tool_call` — **one per tool the assistant ran**,
  with `tool`, `ok` and truncated `args`; this is how you learn it called
  `search_store` with `tags:["Co-op","Rogue-like"]` rather than guessing from
  the reply. Nothing before 2026-08-14 has it. Also `tool_refused` (the
  boundary rejecting a call, e.g. `reason=unknown_appid`), `job_requested`
  (the background brief), `web_search` (provider-executed search).
- **voice, couch verbs**: `nav_dispatched` `quit_dispatched` — both carry the
  host's `answer`, and `FAILED:1` on either means the PC-side scheduled task
  is not registered, not that the verb is broken. Plus `collection_resolved`
  `collection_miss` `install_queued` `install_error` `download_status_error`
- **library**: `sync_done` / `sync_skipped` (both carry `layer` — installed,
  collections, owned, meta, deals), `deals_synced` `meta_fetched`
  `store_fetch_failed` `hltb_failed`
- **listener**: `chord` `chord_busy` `puck_present` `puck_vanished` `puck_standoff` `armed` `heartbeat`
- **supervisor**: `start` `restart` `lane_started` `lane_reloaded` `deps_installed`
- **jobs**: `job_queued` `job_running` `job_done` `job_failed` `job_announced`
- **gamepc**: `enter_start` `profile_applied` (carries `retried` on Enter - true means the first TV-GAMING apply missed and the retry rescued the launch) `profile_retry` `puck_claimed` `ready` (carries `focused`, `fg` = the window that actually held the foreground, and `running_appid` = a game already up at Enter, 0 if none) `enter_failed` `exit_done` `game_launched` `nav_fired` (carries the `steam://` url) `nav_failed` `game_stopped` (carries `method` — `app_stop` / `wm_close` / `kill` / `already-gone`, i.e. WHICH escalation rung actually quit it, plus `cleared`) `game_stop_failed`

Event names are a closed vocabulary and never contain variable data — an
appid or a score is always a field.

Two fields that mislead if read at face value:

- **`ack` on `exlink_send` is not confirmation.** It means the TV's serial
  receiver accepted the frame; Ex-Link here is send-only and nothing reads TV
  power back. A `power_on` can ack and leave the set dark — that is exactly
  what happened on 2026-08-13.
- **`primary_height` on `enter_failed`** separates the two failure shapes: the
  desk's own height means the TV never came up, while anything else (or `-1`)
  means the failed apply detached the desktop and left no active display.

When a launch fails on the profile, the gaming PC now also copies the
interesting lines of DisplayMagician's own log next to the transcript, so it
ships under `lane="pc-transcript"` like everything else in that folder:

```logql
{service="gamepc", lane="pc-transcript"} |= "DisplayMagician"
```

## How to answer well

1. **Start narrow, widen if empty.** `--since 6h` is the default; go wider
   only when it returns nothing.
2. **Lead with the finding, not the query.** "The 9:14pm launch failed because
   the PC never reported READY — the Enter task threw on the TV-GAMING
   profile" beats pasting 40 log lines.
3. **Quote the few lines that prove it**, not the whole result.
4. **`dur_ms` is measured from the chord or wake word**, not from when that
   step started. It is elapsed-since-intent, so it always increases down a
   launch.
5. **A quiet result can be the answer.** No `heartbeat` means a dead lane; no
   `launch_failed` means launches are fine. Say so rather than reporting
   "nothing found".
6. **`couch.log` on the K15 is the offline mirror.** If Loki has a gap, the
   local JSONL (`k15/logs/k15-*.jsonl`) is the source of truth — lines read
   while the shipper was failing are dropped, not queued.

## When a query returns nothing

Check, in order: is `env="prod"` filtering out what you want; is the time
range too narrow; is the field name right (`| json |` present?); has the
gaming PC's shipper been installed (E4). A 401 means the token lacks
`logs:read` — the shipper's write token gives exactly that error.
