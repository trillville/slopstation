# Grafana — implementation guide

Everything needed to take the couch system from "logs are arriving" to "it
tells me when something breaks." Design rationale lives in
[observability-design.md](observability-design.md); this is the doing.

**Status:** ingestion is live (E2). Alerts, dashboards, and the gaming PC's
shipper are not. This guide covers all three.

## This rig's values

Not secrets, so they live here rather than in a password manager. The only
secret is the token, and it exists in exactly one place: a machine
environment variable on the K15.

| | |
|---|---|
| Stack | `narrownuthatch2355` · US West (`prod-us-west-0`) |
| Grafana | `https://narrownuthatch2355.grafana.net` |
| Loki push URL | `https://logs-prod-021.grafana.net/loki/api/v1/push` |
| Loki user (instance id) | `1730320` |
| Loki datasource uid | `grafanacloud-logs` |
| Tempo datasource uid | `grafanacloud-traces` (unused until E5) |
| Access policy | `slopstation-write` — `logs:write`, `traces:write` |
| Alloy config (K15) | `%PROGRAMFILES%\GrafanaLabs\Alloy\config.alloy` |
| Alloy env vars | `GC_LOKI_USER`, `GC_LOKI_TOKEN` (Machine scope) |
| Alloy debug UI | `http://localhost:12345` (K15, localhost only) |

**Where the Cloud portal hides:** grafana.com → **Cloud portal** → **Stacks**
→ your stack's **Details** → *Manage Stack*. Loki, Tempo, and OpenTelemetry
tiles are all on that page. It is *not* reachable from the in-stack "Getting
started" guide, which is where you will instinctively look.

## The data model, in one screen

Four **labels** (cheap to select on, deliberately low-cardinality):

| Label | Values |
|---|---|
| `service` | `k15`, `gamepc` |
| `lane` | `voice`, `launch`, `listener`, `library`, `jobs`, `supervisor`, `traces`, `drill`, `enter`, `exit`, `launchgame`, `pc-transcript` |
| `level` | `debug`, `info`, `warn`, `error` |
| `env` | `prod`, `test` |

Everything else is a **field inside the JSON line** — `event`, `turn`,
`session`, `dur_ms`, `err`, `appid`, `score`, and so on. Fields need
`| json |` to filter on; labels do not. That split is why the free tier stays
free: a `turn` promoted to a label would mint a new stream per launch.

```logql
{service="k15", level="error"}                    # label select - fast
{service="k15"} | json | turn="9f2c1a"            # field filter - the whole story
```

## Part A — deploy the heartbeat (do this first)

Alerts for "is it alive" need something to be absent. Nothing emitted a
heartbeat until now.

On the K15:

```
git pull
k15\Start-K15.bat
```

Confirm within two minutes:

```
python -c "import events,json,pathlib; f=sorted(pathlib.Path('logs').glob('k15-*.jsonl'))[-1]; print([json.loads(l)['lane'] for l in f.read_text(encoding='utf-8').splitlines() if '\"heartbeat\"' in l][-4:])"
```

You want `['listener', 'voice', ...]` — **both lanes**. One lane ticking means
the other did not start, and its alert would fire immediately (correctly).

Heartbeats go to the JSONL only, never to `couch.log` or the console. A line a
minute would be ~1440/day of noise in the one file a human reads.

## Part B — alert rules

Six rules in [`grafana/alerts.yaml`](../grafana/alerts.yaml), in two groups:
`couch-liveness` (2) and `couch-failures` (4).

### The one thing to get right

The two kinds of rule need **opposite** no-data handling:

- **Absence** (`Chord lane down`, `Voice lane down`) → **No data = Alerting**.
  A dead lane emits nothing, so the query returns *no series* and the `< 1`
  threshold never evaluates. These fire through no-data handling; the
  threshold is only a backstop. Set these to OK and the two most valuable
  alerts in the system are permanently inert — **and they look perfectly fine
  in the UI**, which is why drill 1 below is not optional.
- **Presence** (`Launch failed`, `Crash loop`, `Error burst`, `TV not
  answering`) → **No data = OK**. No failures is good news. Set these to
  Alerting and you get paged every quiet night.

### Importing

Try, in order:

1. **Alerting → Alert rules → New alert rule → Import** (accepts YAML in
   recent Grafana). Paste `grafana/alerts.yaml`.
2. **Provisioning API**, if the UI has no import. From any machine, with a
   token that has admin rights on the stack:
   ```
   Invoke-RestMethod -Method Post -Uri "https://narrownuthatch2355.grafana.net/api/v1/provisioning/alert-rules/export" -Headers @{Authorization="Bearer <token>"}
   ```
   Use that to see the canonical shape your Grafana version expects, then POST
   the rules to `/api/v1/provisioning/alert-rules`.
3. **By hand**, from the table below. Six rules, ~5 minutes each, and
   guaranteed to work. Grafana's alert provisioning schema shifts between
   versions; the UI is always authoritative.

**If you create by hand, then export.** Alerting → the rule group → **Export**
→ YAML, and commit it over `grafana/alerts.yaml`. That makes the repo match
reality instead of my best guess at it.

### The rules, for hand-entry

All use datasource `grafanacloud-logs`, query type **Instant**, then
**Reduce (Last)** → **Threshold**.

| Rule | Query | Fires when | No data | For | Severity |
|---|---|---|---|---|---|
| Chord lane down | `sum(count_over_time({service="k15", lane="listener", env="prod"} \| json \| event="heartbeat" [5m]))` | `< 1` | **Alerting** | 2m | critical |
| Voice lane down | `sum(count_over_time({service="k15", lane="voice", env="prod"} \| json \| event="heartbeat" [5m]))` | `< 1` | **Alerting** | 5m | warning |
| Launch failed | `sum(count_over_time({service="k15", env="prod"} \| json \| event="launch_failed" [5m]))` | `> 0` | OK | 0m | warning |
| Crash loop | `sum(count_over_time({service="k15", lane="supervisor", env="prod"} \| json \| event="restart" [10m]))` | `> 3` | OK | 0m | critical |
| Error burst | `sum(count_over_time({service="k15", env="prod", level="error"} [10m]))` | `> 5` | OK | 0m | warning |
| TV not answering | `sum(count_over_time({service="k15", env="prod"} \| json \| event="exlink_nak" [5m]))` | `> 0` | OK | 0m | warning |

Folder `slopstation`, evaluation interval `1m`.

## Part C — where alerts go

Alerting → **Contact points** → the default `grafana-default-email` already
points at your account address. That is enough to start.

Email latency is minutes, which is fine for "the K15 died overnight" and poor
for "the launch failed just now while I am on the couch." If that bites, add a
webhook contact point to [ntfy.sh](https://ntfy.sh) — free, no account, and it
pushes to a phone in seconds:

- Contact point type **Webhook**, URL `https://ntfy.sh/<a-secret-topic-name>`
- Install the ntfy app, subscribe to the same topic

Pick an unguessable topic name; ntfy topics are public to anyone who knows the
string.

**Notification policy.** Optional but worth it: route `severity=critical`
(chord lane down, crash loop) to the fast channel and let the rest go to
email. Otherwise the two alerts that mean "the house is broken" arrive in the
same stream as "a metadata fetch failed."

## Part D — dashboards

Two, in [`grafana/dashboards/`](../grafana/dashboards/). Import via
**Dashboards → New → Import → Upload JSON file**.

- **`house-status.json`** — the one to open first. Both lanes' liveness,
  errors in 24h, launches in 24h, an error/warning bar chart, and a log panel
  of everything at `warn` or above. Answers "is it up, and did anything break
  while I was away?" and nothing else.
- **`launch-health.json`** — launches and failures over 7 days, median and p95
  time-to-READY, per-launch scatter, and a milestone breakdown (`ssh_up` →
  `enter_dispatched` → `host_ready`) so a slowdown can be attributed to a
  stage rather than guessed at.

Both hardcode `"uid": "grafanacloud-logs"`. If you ever rebuild the stack and
the uid changes, find-and-replace it in both files.

The p95 panel is worth watching over time: the build guide measured warm
enters at 6–8 s and wake-from-sleep at 8–13 s, **once, by hand**. This turns
that into something that can be seen drifting.

Not built yet, deliberately:

- **Voice health** (wake→gate→LLM ratios, turn latency) — buildable now, but
  worth waiting until there is a week of real usage to shape the panels
  around.
- **Spend** — blocked. Token counts are computed in `assistant.py` and thrown
  away; they become fields at E5. A dashboard now would show empty panels that
  look broken.

## Part E — the gaming PC (E4)

The correlation query only returns the K15 half of a launch until this ships.

On the **gaming PC**, in an elevated PowerShell:

```
winget install GrafanaLabs.Alloy
```

```
Copy-Item C:\Users\tillm\projects\slopstation\gaming-pc\alloy\config.alloy.example "$env:ProgramFiles\GrafanaLabs\Alloy\config.alloy"
```

```
[Environment]::SetEnvironmentVariable('GC_LOKI_USER','1730320','Machine')
```

```
[Environment]::SetEnvironmentVariable('GC_LOKI_TOKEN','<same token as the K15>','Machine')
```

```
Restart-Service Alloy
```

That config ships two streams on purpose: `pc-*.jsonl` (milestones, carries
the turn) and the `Start-Transcript` narrative as free text under
`lane="pc-transcript"`. The transcript is what you read when an enter fails in
a *new* way — it captures the lines nobody thought to instrument.

Paths in that config assume `C:\CouchGaming\logs`. Verify with
`Get-ChildItem C:\CouchGaming\logs` before restarting the service.

**While you are here, take the clock-skew measurement** (design doc open
question #5). Run on both machines within a few seconds:

```
(Get-Date).ToUniversalTime().ToString('o')
```

Correlation is by `turn`, not timestamp, so skew only misorders a merged view.
If it is under a second, close the question and move on.

## Verification drills

Run these in order. Each proves one thing.

**1. The absence alerts actually fire.** *(The important one.)* On the K15,
stop the voice supervisor window and kill the agent. Within ~6 minutes you
should get a notification. Restart with `Start-K15.bat` and confirm it
resolves. **An alert that never fires looks identical to one that is
working** — this is the only way to tell them apart.

**2. Correlation across machines.** Chord-launch a game, take the `turn` from
the console, then in Explore:
```logql
{service=~"k15|gamepc"} | json | turn="<that turn>"
```
K15 and gaming PC lines, one ordered story. Requires Part E.

**3. Drill noise stays home.** Run the blind suite on the K15, then query
`{service="k15", env="test"}`. Should return **nothing** — test events go to
`test-*.jsonl`, which the shipper's glob excludes. This is the
`WinError 183` problem proving itself fixed.

**4. Secrets do not leak.** On the K15:
```
python -c "import cglib,pathlib,json; ks=[v for k,v in cglib.load_secrets().items() if cglib.real_key(v)]; hits=[l for f in pathlib.Path('logs').glob('*.jsonl') for l in f.read_text(encoding='utf-8').splitlines() if any(k in l for k in ks)]; print('LEAKS:', len(hits))"
```
Must print `LEAKS: 0`.

**5. Crash loop pages.** Break `chord_listener.py` (a bad import will do), let
`Start-Listener.bat` restart it four times, confirm the alert fires. Then
revert. This is the failure that crash-looped the voice agent on 2026-08-11
with no signal but a console.

## Query cookbook

```logql
{service="k15"} | json | turn="9f2c1a"          # one launch, both machines
{service="k15", level="error"}                   # everything broken
{service="k15", lane="voice"} | json | event="gate_miss"   # what the grammar missed
{service="k15"} | json | event="host_ready" | line_format "{{.dur_ms}}ms"
{service="gamepc", lane="pc-transcript"}         # the PC's raw narrative
sum(count_over_time({service="k15", lane="voice"} | json | event="wake" [24h]))
```

Gate-match vs LLM-fallback ratio, the number that says whether the grammar is
earning its keep:

```logql
sum(count_over_time({service="k15", lane="voice", env="prod"} | json | event="gate_match" [24h]))
/
sum(count_over_time({service="k15", lane="voice", env="prod"} | json | event=~"gate_match|gate_miss" [24h]))
```

## When nothing arrives

In this order — the first two cost seconds and the third is where the answer
actually is.

1. **`http://localhost:12345`** on the K15 → `local.file_match.events` →
   **Exports → targets**. Empty means the path or glob is wrong; nothing
   downstream matters.
2. **Access Policies** → the token row → **`Last used at`**. `Never` means the
   token is not reaching Grafana at all, which separates "wrong value" from
   "not being sent" — a distinction no client-side symptom can make.
3. **The Alloy log.** Component health says *Healthy* through hundreds of
   rejected pushes; it means "started", not "working". This has the actual
   HTTP status and Loki's own error text:
   ```
   Get-WinEvent -LogName Application -MaxEvents 60 | Where-Object { $_.ProviderName -like '*Alloy*' -and $_.Message -like '*error*' } | Select-Object -First 5 TimeCreated, Message | Format-List
   ```

Known meanings:

| Symptom | Cause |
|---|---|
| `401 ... "invalid scope requested"` | Token is from a read-only policy. Needs one with `logs:write`. |
| `401 ... "invalid token"` | Value is wrong — or Alloy started *before* the token existed. It reads its environment once, at process start. |
| Healthy everywhere, no data | Nothing new has been written since the last restart. Alloy's position file tracks what was **read**, not what was **sent**: lines read during an outage are dropped, not queued, and never backfill. Emit a fresh line. |
| Explore shows nothing | Datasource defaults to **Prometheus**. Switch to `grafanacloud-narrownuthatch2355-logs`, and switch the editor to **Code**. |

That last row in the third column is the standing trade: **the local JSONL and
`couch.log` are the source of truth, Grafana is a mirror.** Nothing that
matters may live only in the cloud.
