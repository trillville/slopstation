# slopstation

One-chord couch gaming console: RTX 4090 gaming PC → direct HDMI → Samsung S90C,
orchestrated by a GMKtec K15 mini PC. Hold Steam + right trigger on the Steam
Controller for 2 s and the K15 powers the TV on, wakes the PC, flips displays to
TV-only, claims the controller Puck over VirtualHere, launches Big Picture, and
only then switches the TV input — one visible transition, and any failure before
READY leaves the TV exactly where it was.

Or skip the chord: **"hey jarvis, play armored core six"** does the same launch
by voice — wake word on-device, grammar-first command matching, an LLM assistant
lane for everything else ("what should I play tonight?"), TV volume/input/mute
included. Voice is an overlay, never load-bearing: the chord listener is a
separate process and survives anything the voice stack does.

## Docs

Three that are a thing you *do* — how to build it, how to bring voice up, how
to fix it — plus a note per piece of unfinished work, each deleted the day its
feature lands. **Why** the code is shaped as it is lives in the code: the
comments carry the incidents and constraints, so there is no design doc to
drift away from them.

| | |
|---|---|
| [couch-gaming-guide.md](docs/couch-gaming-guide.md) | **Start here to build one.** Physical install → TV settings → VirtualHere gate → display profiles → sshd + tasks → orchestrator → the chord. One-time commands, failure drills, acceptance checklist. |
| [voice-testing.md](docs/voice-testing.md) | Voice bring-up: keys, venv, devices, then escalating drills from a safe dry run to live dispatch. Also the ReSpeaker mic-array bring-up and its accept/reject bar. |
| [troubleshooting.md](docs/troubleshooting.md) | Both lanes, symptom → diagnosis → fix. |
| [custom-wakeword-design.md](docs/custom-wakeword-design.md) | Unfinished: a bespoke wake model is trained and vendored but inert. The two config values that deploy it, and the couch ladder that has to pass before it stays. |
| [resume-game-design.md](docs/resume-game-design.md) | Unbuilt: landing back *in* a game across sessions. Two attempts, why both failed, and the one question that gates a third. |
| [`.claude/skills/`](.claude/skills/) | Two skills so telemetry can be *asked about* rather than looked up: `grafana-logs` (ops — launches, errors, liveness, both machines) and `langfuse-traces` (agent — what the assistant heard, said, cost). Each carries a stdlib-only query script. |

When something misbehaves, troubleshooting is symptom-first; for a full sweep,
run `python doctor.py` on the K15, or on the PC
`powershell -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Doctor.ps1`
(the policy flag is required — the box is `Restricted`, and it is how every task
invokes these scripts anyway). Each diagnoses its whole chain read-only, and
exits with the FAIL count.

## Layout

| Repo path | Runs at | Machine | How |
|---|---|---|---|
| `gaming-pc/` | `C:\CouchGaming\` | `TILLMAN-DESKTOP` (gaming PC) | `Deploy.ps1` (copies the script set, stamps `build-id`) |
| `k15/` | a clone on the Desktop | `K15` (orchestrator mini PC) | `git pull` in place |
| `wake-training/` | in place, from a checkout | `TILLMAN-DESKTOP` (needs the GPU) | `Train.bat` (its venv and 16 GB of data live outside the repo, under `--root`) |

Every script derives its sibling paths from its own location, so a folder is a
relocatable unit and runs fine straight from a checkout. The **K15 runs from a
clone** (`git pull` to update — no copying). The **gaming PC deploys by copy**,
because its runtime needs gitignored binaries/shortcuts (`vhui64.exe`,
`OFFICE.lnk`, `TV-GAMING.lnk`) that can't live in the repo — but the copy is
`Deploy.ps1`'s job, not a hand operation: it ships the scripts as one checked
set, stamps a `build-id`, and never touches the gitignored pieces. The K15's
`doctor.py` compares that stamp against its own checkout (`ssh gamepc
version`), so the two machines drifting apart is a WARN instead of a surprise.

Run the `.bat` files by double-clicking them, or from PowerShell with a
leading `.\` (`.\Start-K15.bat`) — PowerShell will not run anything from the
current directory without an explicit path, unlike cmd.exe.

Per-machine files are **gitignored, created once from committed examples** so a
checkout runs without its local config/keys ever fighting `git pull`:
`config.json` ← `config.example.json`, `secrets.json` ← `secrets.template.json`.

### Gaming PC (`gaming-pc/`)

| File | Role |
|---|---|
| `CouchGaming.common.ps1` | Shared library, dot-sourced by the session/task scripts: `$CG` constants (the `2160` TV sentinel, Puck address/HW ID, paths), display probe, Puck claim/release, profile apply-verify, task guards, ready-marker ops, and the Steam library/install-dir/process resolvers `Stop-Game` uses. |
| `Enter-TV.ps1` | Session setup: TV-GAMING profile, Puck claim, Big Picture, `READY` marker. Task `\CouchGaming\Enter`. |
| `Exit-TV.ps1` | Teardown: close Big Picture, restore OFFICE, release Puck. Task `\CouchGaming\Exit`. Stops a mid-flight Enter first (teardown wins). |
| `Office-Safety.ps1` | Unconditional OFFICE restore at every logon. Task `\CouchGaming\ForceOfficeAtLogon`. Stands down while Enter/Exit run. |
| `Wake-Safety.ps1` | Cleans up sessions abandoned before sleep; stands down for network wakes. Task `\CouchGaming\WakeSafety`. |
| `Dispatch.ps1` | Entire SSH attack surface: `enter` / `exit` / `status` / `enterstate` / `games` / `playing` / `launch <appid>` / `version` / `nav <kind> [arg]` / `stop <appid>` / `collections`, everything else `DENIED`. Forced command in `administrators_authorized_keys`; deliberately dependency-free. |
| `Launch-Game.ps1` | Task `\CouchGaming\LaunchGame`, fired by the `launch` verb: reads the appid marker, re-validates, `steam -applaunch` into the running Big Picture session. |
| `Nav-BigPicture.ps1` | Task `\CouchGaming\Nav`, fired by the `nav` verb: reads the nav-target marker, re-validates, fires a `steam://` URL (downloads/library/store/game page/collection) into Big Picture. |
| `Stop-Game.ps1` | Task `\CouchGaming\StopGame`, fired by the `stop` verb: quits the running game — `steam +app_stop`, then window-close, then a forced tree-kill only if both are ignored — and re-focuses Big Picture after. |
| `Deploy.ps1` | The deploy: copies the script set from a checkout to `C:\CouchGaming\` and stamps `build-id` (what the `version` verb answers). Refuses a partial set; never touches the gitignored runtime pieces. |
| `Doctor.ps1` | On-demand chain diagnosis: files, tasks, sshd/firewall/key ACL, VirtualHere, display probe, session state. Read-only; exit code = FAIL count. |

### K15 (`k15/`)

| File | Role |
|---|---|
| `cglib.py` | Shared module: Ex-Link frame table, Puck VID/PID, config loading, and the per-lane logger (`log("event", field=…)` → console + `couch.log` + structured JSONL). |
| `events.py` | The event core, stdlib-only so the chord lane gains no dependency: JSONL writer, daily files, secret scrubber, the `turn` correlation id, and an `emit` CLI the `.bat` supervisors call so a crash-restart loop is visible. |
| `couch.py` | Orchestrator: Ex-Link TV power → WoL → `ssh enter` → poll READY → switch input → watch loop. `reconcile` subcommand re-adopts or clears a session lock that survived a K15 restart. |
| `chord_listener.py` | Watches the Puck's HID stream for Steam + right-trigger held 2 s and answers through the controller — 1 thud = launching, 2 = busy, 3 = the launch failed — then fires `couch.py start`. Logs to `couch.log` as `[listener]`. |
| `haptic_test.py` | Bench tool for the controller's haptic output reports (chirp/pulse/rumble variants). Run only with the listener stopped; re-run after firmware updates. |
| `doctor.py` | On-demand chain diagnosis: config, deps, Ex-Link port, Puck, listener, haptics (auto-skipped while the listener owns the Puck), ssh contract, session state, voice overlay (WARN-only). |
| `exlink.py` | Manual Ex-Link TV control: power/inputs/volume/mute; COM port from config. |
| `library.py` | Game catalog: installed (over ssh), owned + metadata (Steam Web API, key-gated), collections (over ssh), merged into `state/library.json`; auto-synced by the voice agent. Also the layer-4 store-question fetchers (search, wishlist-on-sale, specials, trending, reviews, news, how-long-to-beat) and the `state/deals.json` precompute the voice tools and the worker read. |
| `calibrate.py` | Rediscovers the controller's HID button bytes after firmware changes. |
| `config.json` | Orchestrator config (MAC, IPs, COM port, input mapping, voice tuning). |
| `Start-TV-Gaming.bat` | Manual recovery launcher: runs `couch.py start` in a window that stays open. |
| `Start-K15.bat` | **The** Startup-folder shortcut target, and the one thing to run after a `git pull` — both converge on "both lanes running current code". Per lane: supervisor down → start it; supervisor up → kill the *agent* so its supervisor relaunches it on new code. Never bounces a supervisor window, so a live session's watch loop (and `reconcile`) are untouched. |
| `Start-Listener.bat` | Chord-lane supervisor: `reconcile` once, then the listener in a 10 s restart loop. Single-instance (a second launch bounces). |
| `voice/` | The voice overlay: `voice_agent.py` (composition root + wake loop), `audio.py` (the PortAudio world: devices, recovery, wake listener), `session_runtime.py` (one session's Pipecat pipeline: Flux STT → grammar gate → dispatch, with an LLM assistant lane, + cross-session carry), `grammar.yaml` + `titles.py` (command grammar + fuzzy title/collection resolution), `dispatch.py` (every voice side effect — session, TV, launch, quit, Big Picture nav — shared by both lanes, + the per-utterance snapshot), `preroll.py` (no-pause wake buffer), `assistant.py` (catalog-in-context brain, the store-data + nav + quit + install tools, optional web search, + `--text` REPL), `steam_session.py` (optional signed-in account session: install-by-voice + download status over ClientComm, token-gated), `workers.py`/`jobs.py`/`announce.py` + `worker_home/` (Tier-3 background tasks: claude/codex CLI adapters, job store, proactive spoken results), `tests/` (blind suite). Own venv, own pins. |
| `voice/Start-Voice.bat` | Voice-lane supervisor (launched by `Start-K15.bat`): creates the venv on first run, then supervises the agent (single-instance, 10 s crash restart). The dependency gate lives *inside* the restart loop and compares `requirements.txt` against a copy of itself, so a `git pull` that changes pins installs them on the next agent launch rather than needing a cold start. |

## Code architecture

- **Shared code lives in one place per machine**: `CouchGaming.common.ps1`
  (dot-sourced) and `cglib.py` (imported). Every magic value — the `2160`
  sentinel, `K15.5`, hardware IDs, Ex-Link frames, timeouts — has exactly one
  home.
- **The `2160` sentinel** (`Test-TvIsPrimary`): "primary display height equals
  the TV's" is how every PC script reads the topology. It holds only while the
  desk monitor's height differs — revisit before pairing this rig with a 4K or
  5K2K (5120×2160!) desk monitor.
- **Every piece of distributed state has a reconciler**: the ready marker
  (Office-/Wake-Safety), display topology (Office-Safety at every logon), stale
  Puck claims (Enter recycles, and aborts if the recycle won't verify), the
  session lock (heartbeat + staleness recycling + `reconcile` after a K15
  restart).
- **Conflict rule**: teardown wins, launch queues, safety stands down — Exit
  stops a running Enter; Enter waits briefly for a running Exit then aborts;
  Office-Safety does nothing while either runs.
- **Verification over reports**: Windows device enumeration is the arbiter of
  claim state (VirtualHere's IPC report lies); profile applies are verified by
  the probe, and DisplayMagician is killed after every apply.
- **The one rule**: nothing switches the TV to the gaming input before the host
  writes `READY` — every pre-READY failure leaves the TV as the viewer had it.
- **When to extract a module** (and when not): cut a new file only when one of
  these is true — (a) the concept has accumulated its own incident history and
  failure domain (how `voice/audio.py` earned its name), (b) a duplication
  exists only to break an import cycle, or (c) the file no longer fits in one
  sitting. Never to satisfy a diagram, and never speculatively. `cglib.py`'s
  section headers are the pre-drawn fault lines: split along them when size
  actually hurts, not before.
- **Moves are pure**: a commit that relocates code changes zero behavior, and
  every incident-history comment travels with its code — the comments are this
  repo's institutional memory. Land behavior changes first, moves second,
  never both in one commit.
- **Design docs are for unbuilt work.** One exists while a thing is being
  decided and is deleted when it ships — the code and its comments become the
  record, and a doc that outlives its feature only drifts.

## Deliberately not doing

Closed questions, kept so they are not re-opened for free. Each was decided
with the reasoning; if the premise changes, reopen it deliberately.

| | |
|---|---|
| **Services, a database, or merging the voice and chord lanes** | The process split *is* the failure isolation. File-backed state a human can inspect and delete at 2am is the feature. |
| **Session phases/snapshots on disk** | Optimistic software state. The probes — display, PnP, Steam registry, ready marker — already answer every phase question truthfully. The lock got atomicity and identity, not a state machine. |
| **A typed config layer, a `pc_client`/`tv` extraction, splitting `cglib.py`** | The verb responses mean different things per call site; presence-checks plus `config_suspect` warnings catch the failure that actually occurs. Revisit only when size hurts. |
| **JSON envelopes for the PC marker files** | Four one-line files (`launch-app`, `nav-target`, `stop-app`, `turn`), each written and read by code that sits either side of one `schtasks` call. Every one of them is re-validated downstream anyway, and a lost turn costs a log label. |
| **Self-hosting telemetry on the K15** | It is the thing being observed, and it runs a latency-sensitive audio pipeline. A dashboard that dies with its subject is not a dashboard. |
| **Sentry, a metrics SDK, span sampling, session replay, audio upload** | This code catches almost everything by design, so Sentry would receive very little; the rest do not earn their complexity at one household's volume. Revisit Sentry if unhandled `voice_agent` crashes become a theme. |
| **Tracing library sync and metadata crawls** | Those are logs. Nobody is waiting on them. |
| **A custom status page** | Grafana and Langfuse are the web app. If one is ever wanted it reads their query APIs and stores nothing. |
| **Packaging `k15/voice` (pyproject, installs)** | Double-clicking a `.bat` from a checkout is the product. The `sys.path` inserts are the price and it is already paid. |
| **Pausing/resuming downloads by voice** | Voice can *see* download progress (ClientComm `download_status`) and *start* installs, but not pause/resume. Deliberately cut: it is a rare want, and the couch is for playing, not managing a queue. The CDP `Downloads.*` verbs exist if this is ever revisited. |
| **CEC for TV power (a Pulse-Eight adapter), or hunting an Ex-Link status frame** | The set's own `/api/v2/` endpoint answers the power question free over the LAN (`cglib.tv_power_state`; couch.py's TV-wake gate is the consumer). CEC is ~$50 of hardware plus libcec plus a relay verb, and Samsung's own partners report Anynet+ disrupts IP control — it could cost the channel that solved this. The Ex-Link ack was probed 2026-08-19: a constant `030cf1` regardless of power state or command, nothing past three bytes. Do not re-run that probe, and do not brute-force the command space hunting a status frame — the same protocol carries service-mode commands, and a valid-checksum guess is not safe to fire at a TV someone watches. |

Still genuinely open, worst first:

- **The worker's output reaches the assistant as the assistant's own words.**
  Text derived from untrusted web pages is seeded into the next conversation as
  `role: "assistant"`, in a context that can quit a game or queue an install.
  Hardening the worker's *tools* (done) does not touch this, because the channel
  is its *output*. Bounded — quit confirms first, install validates ownership,
  `bench/probe_intent.py` measures that a question never becomes an action — but
  not closed. The mitigation and the measurement that would prove it are on
  `voice/session_runtime.py`'s `job_messages`.
- **Whether the `codex` worker lane earns its keep.** It is not the configured
  provider, it has none of the claude lane's isolation (its sandbox confines
  writes, not reads or shell — `doctor.py` warns whenever it is selected), and
  dropping it would delete a risk nobody is using. Kept only as the A/B arm.
- **The TV wake itself.** The launch path now reads the set's own PowerState —
  Enter waits for an answered "on", and a set that keeps refusing fails the
  launch early with the TV named (couch.py's `wait_tv_on`) — but nothing yet
  makes the refusal *rarer*. Two unmeasured levers, both from Samsung's IP
  Control Worksheet: the standby-depth settings (eco, "Power On with Mobile",
  "Keep Bixby in Standby" — the last is described as holding the IP server
  open in standby precisely to make power-on reliable) are menu toggles whose
  measurement is the failure rate itself; and the TV answers WoL — a second
  wake channel is a MAC in config (`68:FC:CA:B4:02:22` here, though that is
  the wireless MAC and WoL over Wi-Fi is the weaker case) and one more
  `wol()` call beside the Ex-Link frame. The free measurement is already
  wired: `launch_start`'s `tv` field logs the raw depth rung ("on" /
  "standby" / `""`-deep / null-unreachable), so whether `""` predicts a
  refused wake is now a Grafana query, not a project.
- **Clock skew** between the two machines — correlation is by `turn` rather than
  timestamp, so skew only misorders a merged view; measure it by running
  `(Get-Date).ToUniversalTime().ToString('o')` on both within a few seconds and
  close the question if it is under a second.
- **Tempo dual-export** (`TODO(E5b)` in `voice/tracing.py`) and **barge-in**,
  which pipecat 1.7 does not give us for free — the mechanism and the revival
  recipe are in `voice/session_runtime.py`.

## Deliberately not in the repo

- **VirtualHere binaries** (`vhui64.exe` client on the PC, `vhusbdwinw64.exe` server on the K15) — download from virtualhere.com.
- **VirtualHere `config.ini`** — contains the EasyFind ID/PIN, which are remote-access credentials. Never commit.
- **`OFFICE.lnk` / `TV-GAMING.lnk`** — machine-generated DisplayMagician profile shortcuts; recreate per guide Stage 6.
- **`k15/config.json`, `k15/secrets.json`** — per-machine config and API keys; create once from `config.example.json` / `secrets.template.json`. Gitignored so a checkout runs without them fighting `git pull`.
- **Scheduled task registrations, sshd setup, firewall rules** — one-time commands, all in the guide (Stages 6–8).
- **Logs and runtime state** (`logs/` — both the PC's transcripts and the daily `*.jsonl` event stream — plus `state/session.lock` and `couch.log`), and the voice `.venv` (created on-machine).

## Conventions

| Item | Value |
|---|---|
| Gaming PC | `TILLMAN-DESKTOP` · `192.168.68.67` · MAC `74-56-3C-45-92-DD` · user `tillm` |
| K15 | `K15` · `192.168.68.75` · user `minipc` |
| Puck (VirtualHere) | `K15.5` — VID `28DE`, PID `1304` |
| Ex-Link serial | `COM3` on the K15 · 9600 8N1 |
| TV inputs | HDMI1 Apple TV · HDMI2 PS5 · HDMI3 eARC · HDMI4 PC |
| TV EDID name | `QCQ90S` |
| Remote surface | `ssh gamepc enter\|exit\|status\|games\|playing\|launch <appid>\|version\|nav <kind> [arg]\|stop <appid>\|collections` — nothing else exists |
| The one rule | Nothing switches the TV to HDMI 4 before the host writes `READY` |
