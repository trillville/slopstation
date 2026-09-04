# Operations

Deploying, checking, diagnosing and recovering the running system. Setting it
up is [setup.md](setup.md); the values it reads are in
[configuration.md](configuration.md).

## Deploying

A green `ci` run on `main` starts `cd` (`.github/workflows/cd.yml`). Two
independent jobs run on the self-hosted runners:

- **Mini PC.** `python -m slopstation.deploy --sha <sha>` runs from the live
  checkout. It waits up to two hours for a live session to end, fast-forwards
  the checkout to the commit, restarts the lanes (installing changed pins
  first), starts the media stack if enabled, and runs the doctor. Its exit
  code is the doctor's failure count.
- **Gaming PC.** `gaming-pc\Deploy.ps1 -WaitMinutes 120` from the runner's
  checkout copies the script set into `C:\CouchGaming`, stamps `build-id`,
  and runs `Doctor.ps1`. It never writes `config.psd1`.

Both wait for a live session and fail rather than interrupt one. Neither rolls
back. The gaming-PC job queues while that machine is asleep and runs when it
wakes. Because the repository is public, `cd` deploys only commits pushed to
`main`; pull-request code never reaches either runner.

Hand deploys are the same two scripts:

```powershell
# mini PC, from a NORMAL window (an elevated lane cannot be stopped by the deployer)
git pull
.\Start-Slopstation.bat
.venv\Scripts\slopstation-doctor

# gaming PC, from a checkout on the PC
powershell -NoProfile -ExecutionPolicy Bypass -File .\gaming-pc\Deploy.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Doctor.ps1
```

Rules the deployer enforces:

- `deploy.py` refuses a live checkout that is dirty in tracked files or not
  on `main`. A checkout left on a branch has disabled CD until someone
  switches it back.
- `deploy.py` runs the previous commit's copy of itself, because it is the
  thing that updates the checkout. A change to it takes effect one deploy
  later, and a new deployer must be pulled by hand once.
- The live checkout never receives a commit. To ship something produced on
  the mini PC, such as a refrozen `constraints.txt`: `git switch -c <branch>`
  first, commit, push, `git switch main`. `git status` must then read "up to
  date with origin/main"; "ahead by 1" means the fast-forward merge will
  refuse every deploy until the commit is gone.
- Pull requests are squash-merged.

### What CD does not do

Each of these fails at the doctor rather than at the thing that broke, so a
deploy goes red with a diagnosis, and someone does the step by hand.

- **`constraints.txt`** is a `pip freeze` from the mini PC's venv, committed by
  hand; no other machine can produce it. The lane's install gate hashes
  `pyproject.toml` and `constraints.txt` against a `deps-ok` sentinel in the
  venv, so a change to either installs itself on the next deploy. Regenerate
  it on the mini PC with
  `.venv\Scripts\pip freeze --exclude-editable | Out-File -Encoding ascii constraints.txt`,
  not `>`, whose output under PowerShell 5.1 is UTF-16 and unreadable to pip.
- **New `config.json` keys.** A key added to `config.REQUIRED` or
  `config.REQUIRED_VOICE` is a hand edit on the mini PC.
- **New `config.psd1` keys.** The same on the gaming PC, in
  `C:\CouchGaming\config.psd1`.
- **Scheduled tasks.** The mini PC's two lane tasks point at
  `.venv\Scripts\slopstation-lane.exe` by absolute path, so moving the
  checkout means re-running `Setup-K15-Tasks.ps1`. The gaming PC's tasks are
  re-registered by `gaming-pc\Install.ps1`, run elevated from a checkout.
- **Runtime pieces on the PC.** `vhui64.exe` and the two DisplayMagician
  shortcuts. `Deploy.ps1` warns when they are missing and never touches them.

## Doctors

Both doctors are read-only, print one row per check, and exit with their
failure count.

- Mini PC: `.venv\Scripts\slopstation-doctor`. Config and secrets, imports, the
  Ex-Link port, the controller, both lanes, SSH to the gaming PC and the
  dispatcher's answer, deploy skew between the two machines (`ssh <sshHost>
  version` against the checkout), VirtualHere and its firewall rule, session
  state, the voice library and keys, the Steam session, and media when
  enabled.
- Gaming PC: `C:\CouchGaming\Doctor.ps1`. The loaded config, the deployed
  files, each scheduled task against its definition, sshd and the mini-PC-only
  firewall rule, the key file's ACL, the NIC's wake settings, VirtualHere and
  the controller's address, the display probe, the TV link, and the session
  markers.

## Diagnosing

Every user intent, a chord press or a wake word, mints a short hex `turn` id.
It travels through the mini PC's events, the SSH verb, the gaming PC's marker
file, and the PC tasks' events and transcript names. Both machines ship their
JSONL event logs to one Sentry project, so one query returns a launch from
the chord to Big Picture:

```
turn:9f2c1a
```

Where to look:

| Question | Where |
|---|---|
| what broke, either machine | Sentry logs, `env:prod severity:[warn,error]` |
| why a launch failed | the `launch_failed` event's `turn`, then that turn |
| why a voice command did nothing | `lane:voice event:[gate_miss,title_miss]`, and the trace for that `session` |
| what the assistant did | the agent view for the `session`: prompts, tools, tokens |
| is a lane alive | the two cron monitors, `k15-listener` and `k15-voice`; a missed check-in pages |
| the PC's own narrative | `service:gamepc lane:pc-transcript` |
| a verb the PC refused | `lane:dispatch answer:DENIED` |

Offline, the same events are on disk: `logs\k15-*.jsonl` on the mini PC (14
days), `C:\CouchGaming\logs\pc-*.jsonl` and `pc-dispatch-*.jsonl` on the PC,
plus one transcript per PC task run with the turn id in its name. The
collector persists its read offsets, so an outage backfills rather than
skipping.

The event vocabulary is closed and frozen in `tests/test_event_names.py`. The
Sentry skill in `.claude/skills/sentry` documents every event and the queries
that answer the usual questions.

## Recovering

| Symptom | Cause | Fix |
|---|---|---|
| the chord does nothing and the doctor's `session lock` row is stale | a launch died without releasing `state\session.lock` | `python -m slopstation.couch reconcile`; the supervisor also runs it once per boot |
| the controller buzzes the failure pattern | `state\last_error` from the last failed launch; discarded after ten minutes | read the launch's turn |
| a launch never reached `READY` | see the turn; `enter_died` means the PC's Enter task ended without the marker, usually the TV not waking | try again; the rescue redispatches once on its own |
| the PC is stuck in the TV profile at the desk | a session ended without Exit | the `Exit` task, or logging on again (`ForceOfficeAtLogon`), or DisplayMagician's own hotkey for the office profile |
| the PC's doctor reports a stale ready marker | a crash left `C:\ProgramData\CouchGaming\ready` | run the `Exit` task |
| the controller is claimed by nobody or by the wrong machine | a stale VirtualHere claim | the `Enter` task recycles it; `Exit` releases it |
| a lane is down after a deploy | the task is not registered or refused to start elevated | `Setup-K15-Tasks.ps1` then `Start-Slopstation.bat`, from a normal window |
| the two machines run different code | the doctor's `deploy skew` row | redeploy the older side |
| a media request is stuck | see `operations list --active` | `operations reconcile`, or `operations abandon <id> --execute` |

## Runtime state on the mini PC

Under `state\`:

| File | Owner | Meaning |
|---|---|---|
| `session.lock` | `couch.py` | a session owns the controller; its mtime is liveness |
| `cancel` | the voice lane | a "end the session" against an in-flight launch |
| `last_error` | `couch.py` | the last failed launch, for the listener to buzz |
| `operations.json` | the operations tracker | every tracked Steam and media job |
| `library.json`, `deals.json`, `metadata-cache.json`, `hltb-cache.json`, `store-tags.json` | the library sync | the game catalog and its caches |
| `traces\` | the voice agent | conversation traces, kept 14 days |

## Commands

```powershell
# talk to the assistant from a terminal
.venv\Scripts\slopstation-text
.venv\Scripts\slopstation-text "what is downloading?"

# tracked Steam and media work
.venv\Scripts\python -m slopstation.agent.tools.operations list --active
.venv\Scripts\python -m slopstation.agent.tools.operations show <operation-id>
.venv\Scripts\python -m slopstation.agent.tools.operations reconcile
.venv\Scripts\python -m slopstation.agent.tools.operations abandon <operation-id> --execute

# the gaming PC, from the mini PC
ssh <sshHost> status
ssh <sshHost> version

# tests, as CI runs them
.venv\Scripts\pytest
.venv\Scripts\ruff check .
.venv\Scripts\mypy
```

Tests that need a local Steam installation skip without one. Tests that open
real audio devices run only with `SLOPSTATION_TEST_AUDIO=1`.
