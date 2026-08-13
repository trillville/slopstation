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

| | |
|---|---|
| [couch-gaming-guide.md](docs/couch-gaming-guide.md) | **Start here to build one.** Physical install → TV settings → VirtualHere gate → display profiles → sshd + tasks → orchestrator → the chord. One-time commands, failure drills, acceptance checklist. |
| [voice-testing.md](docs/voice-testing.md) | Voice bring-up: keys, venv, devices, then escalating drills from a safe dry run to live dispatch. |
| [voice-control-design.md](docs/voice-control-design.md) | Why the voice stack is shaped the way it is — pipeline, alternatives weighed, costs, edges. |
| [troubleshooting.md](docs/troubleshooting.md) | Both lanes, symptom → diagnosis → fix. |
| [`.claude/skills/`](.claude/skills/) | Two skills so telemetry can be *asked about* rather than looked up: `grafana-logs` (ops - launches, errors, liveness, both machines) and `langfuse-traces` (agent - what the assistant heard, said, cost). Each carries a stdlib-only query script. |
| [grafana-implementation.md](docs/grafana-implementation.md) | Setting up alerts, dashboards and the gaming PC shipper: this rig's values, the rules, the drills, and what to check when nothing arrives. |
| [observability-design.md](docs/observability-design.md) | Why it is shaped this way — structured events, the `turn` id, what was weighed and what each phase's build actually found. Logs are live; Langfuse agent traces are still to wire. |
| [architecture-review-2026-08.md](docs/architecture-review-2026-08.md) | The 2026-08 architecture review: verdict (keep it), the six findings and their fixes, what was deliberately declined and why, and the standing risks. Read before re-litigating the session protocol, the worker boundary, or the module layout. |

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
| `CouchGaming.common.ps1` | Shared library, dot-sourced by the four session scripts: `$CG` constants (the `2160` TV sentinel, Puck address/HW ID, paths), display probe, Puck claim/release, profile apply-verify, task guards, ready-marker ops. |
| `Enter-TV.ps1` | Session setup: TV-GAMING profile, Puck claim, Big Picture, `READY` marker. Task `\CouchGaming\Enter`. |
| `Exit-TV.ps1` | Teardown: close Big Picture, restore OFFICE, release Puck. Task `\CouchGaming\Exit`. Stops a mid-flight Enter first (teardown wins). |
| `Office-Safety.ps1` | Unconditional OFFICE restore at every logon. Task `\CouchGaming\ForceOfficeAtLogon`. Stands down while Enter/Exit run. |
| `Wake-Safety.ps1` | Cleans up sessions abandoned before sleep; stands down for network wakes. Task `\CouchGaming\WakeSafety`. |
| `Dispatch.ps1` | Entire SSH attack surface: `enter` / `exit` / `status` / `games` / `playing` / `launch <appid>` / `version`, everything else `DENIED`. Forced command in `administrators_authorized_keys`; deliberately dependency-free. |
| `Launch-Game.ps1` | Task `\CouchGaming\LaunchGame`, fired by the `launch` verb: reads the appid marker, re-validates, `steam -applaunch` into the running Big Picture session. |
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
| `library.py` | Game catalog: installed (over ssh), owned + metadata (Steam Web API, key-gated), merged into `state/library.json`; auto-synced by the voice agent. |
| `calibrate.py` | Rediscovers the controller's HID button bytes after firmware changes. |
| `config.json` | Orchestrator config (MAC, IPs, COM port, input mapping, voice tuning). |
| `Start-TV-Gaming.bat` | Manual recovery launcher: runs `couch.py start` in a window that stays open. |
| `Start-K15.bat` | **The** Startup-folder shortcut target, and the one thing to run after a `git pull` — both converge on "both lanes running current code". Per lane: supervisor down → start it; supervisor up → kill the *agent* so its supervisor relaunches it on new code. Never bounces a supervisor window, so a live session's watch loop (and `reconcile`) are untouched. |
| `Start-Listener.bat` | Chord-lane supervisor: `reconcile` once, then the listener in a 10 s restart loop. Single-instance (a second launch bounces). |
| `voice/` | The voice overlay: `voice_agent.py` (composition root + wake loop), `audio.py` (the PortAudio world: devices, recovery, wake listener), `session_runtime.py` (one session's Pipecat pipeline: Flux STT → grammar gate → dispatch, with an LLM assistant lane, + cross-session carry), `grammar.yaml` + `titles.py` (command grammar + fuzzy title resolution), `dispatch.py` (every voice side effect, shared by both lanes, + the per-utterance snapshot), `preroll.py` (no-pause wake buffer), `assistant.py` (catalog-in-context brain, optional web search, + `--text` REPL), `workers.py`/`jobs.py`/`announce.py` + `worker_home/` (Tier-3 background tasks: claude/codex CLI adapters, job store, proactive spoken results), `tests/` (blind suite). Own venv, own pins. |
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
| Remote surface | `ssh gamepc enter\|exit\|status\|games\|playing\|launch <appid>\|version` — nothing else exists |
| The one rule | Nothing switches the TV to HDMI 4 before the host writes `READY` |
