# slopstation

[![ci](https://github.com/trillville/slopstation/actions/workflows/ci.yml/badge.svg)](https://github.com/trillville/slopstation/actions/workflows/ci.yml)

One-chord couch gaming console. An RTX 4090 gaming PC feeds a Samsung S90C over
direct HDMI; a GMKtec K15 mini PC orchestrates.

Hold **Steam + right trigger** on the Steam Controller for 2 s and the K15
powers the TV on, wakes the PC, flips displays to TV-only, claims the controller
Puck over VirtualHere, launches Big Picture, and only then switches the TV
input. Nothing switches the TV to the gaming input before the PC writes its
`READY` marker, so any failure before that leaves the TV where it was.

Or say **"hey jarvis, play armored core six"**. The voice overlay runs an
on-device wake word, matches commands against a grammar first, and falls back to
an LLM assistant for open questions ("what should I play tonight?"). Voice is a
separate process with its own venv; the chord listener is unaffected by anything
it does.

## Layout

Three units. Each script derives its sibling paths from its own location, so a
folder runs fine straight from a checkout.

| Repo path | Machine | Runs at | Updated by |
|---|---|---|---|
| `gaming-pc/` | `TILLMAN-DESKTOP` | `C:\CouchGaming\` | `Deploy.ps1` from a checkout on the PC |
| `k15/` | `K15` | a clone on the Desktop | `git pull` in place |
| `wake-training/` | `TILLMAN-DESKTOP` (needs the GPU) | in place | n/a — run from a checkout |

The K15 runs from a clone. The gaming PC deploys by copy, because its runtime
needs gitignored binaries and shortcuts (`vhui64.exe`, `OFFICE.lnk`,
`TV-GAMING.lnk`) that can't live in the repo. `Deploy.ps1` ships the script set
as one checked batch, refuses a partial set, and stamps a `build-id`; the K15's
`doctor.py` compares that stamp against its own checkout (`ssh gamepc version`)
and warns on drift.

### Gaming PC (`gaming-pc/`)

| File | Role |
|---|---|
| `CouchGaming.common.ps1` | Shared library, dot-sourced by every other script: `$CG` constants (the `2160` TV sentinel, Puck address/HW ID, paths), display probe, Puck claim/release, profile apply-verify, task guards, ready-marker ops, Steam library/install-dir/process resolvers. |
| `Enter-TV.ps1` | Session setup: TV-GAMING profile, Puck claim, Big Picture, `READY` marker. Task `\CouchGaming\Enter`. |
| `Exit-TV.ps1` | Teardown: close Big Picture, restore OFFICE, release Puck. Task `\CouchGaming\Exit`. Stops a mid-flight Enter first. |
| `Office-Safety.ps1` | Unconditional OFFICE restore at every logon. Task `\CouchGaming\ForceOfficeAtLogon`. Stands down while Enter/Exit run. |
| `Wake-Safety.ps1` | Cleans up sessions abandoned before sleep; stands down for network wakes. Task `\CouchGaming\WakeSafety`. |
| `Dispatch.ps1` | The entire SSH surface — see the verb list under Conventions. Forced command in `administrators_authorized_keys`; dependency-free. Mutating verbs and `DENIED` write a `verb` event to `logs\pc-dispatch-*.jsonl` (its own file: Dispatch runs elevated, the tasks do not). |
| `Launch-Game.ps1` | Task `\CouchGaming\LaunchGame`, fired by `launch`: reads the appid marker, re-validates, `steam -applaunch` into the running Big Picture session. |
| `Nav-BigPicture.ps1` | Task `\CouchGaming\Nav`, fired by `nav`: reads the nav-target marker, re-validates, fires a `steam://` URL into Big Picture. |
| `Stop-Game.ps1` | Task `\CouchGaming\StopGame`, fired by `stop`: `steam +app_stop`, then window-close, then a forced tree-kill only if both are ignored. Re-focuses Big Picture after. |
| `Deploy.ps1` | Copies the script set to `C:\CouchGaming\` and stamps `build-id` (what `version` answers). |
| `Doctor.ps1` | Read-only chain diagnosis: files, tasks, sshd/firewall/key ACL, VirtualHere, display probe, session state. Exit code = FAIL count. |
| `alloy/config.alloy.example` | Grafana Alloy config template — ships `logs\pc-*.jsonl` to Loki. Copy to `config.alloy` (gitignored). |

### K15 (`k15/`)

| File | Role |
|---|---|
| `cglib.py` | The K15 runtime core: `config()` (read once per process) and secrets, the per-lane logger (`log("event", field=…)` → console + `couch.log` + JSONL) and its test double, the session lock and its markers, `load_json`/`write_json` for `state/`, log rotation. |
| `tv.py` | The TV from the K15, chord-safe: Ex-Link frame table and serial send, the HTTP reads `tv_power_state` / `tv_volume`. The venv-side write path over WebSocket is `voice/tv_remote.py`. |
| `haptics.py` | The controller's haptics over the Puck: VID/PID, the 0x42 input report id, report builders, `play_pattern`, the thud vocabulary. |
| `gamepc.py` | The gaming PC as the K15 sees it: `ssh()`/`ssh_intent()` and one function per Dispatch verb (`enter`, `status`, `launch`, …). `test_turn` holds it and `Dispatch.ps1` in step. |
| `events.py` | The event core, stdlib-only so the chord lane gains no dependency: JSONL writer, daily files, secret scrubber, the `turn` correlation id, and an `emit` CLI the `.bat` supervisors call so a crash-restart loop is visible. |
| `couch.py` | Orchestrator: Ex-Link TV power → WoL → `enter` → poll READY → switch input → watch loop. Subcommands `start` and `reconcile` (the latter re-adopts or clears a session lock that survived a K15 restart). |
| `chord_listener.py` | Watches the Puck's HID stream for Steam + right-trigger held 2 s and answers through the controller — 1 thud = launching, 2 = busy, 3 = failed — then fires `couch.py start`. |
| `library.py` | Game catalog: installed games (over ssh), owned games + metadata (Steam Web API, key-gated), collections (over ssh), merged into `state/library.json` and auto-synced by the voice agent; `Catalog` is one read of it for a voice session. |
| `steamstore.py` | Live Steam store data: search, wishlist-on-sale, specials, trending, reviews, news, how-long-to-beat, and the `state/deals.json` precompute. `python steamstore.py <probe>` smokes an endpoint. |
| `doctor.py` | Read-only chain diagnosis: config, deps, Ex-Link port, Puck, listener, haptics (skipped while the listener owns the Puck), ssh contract + deploy skew, session state, telemetry (event retention + the Alloy service), voice overlay (WARN-only, one `check_*` per row group). |
| `slop.py` | Authenticated general text client for the K15 assistant; interactive or one-shot from either machine. |
| `exlink.py` | Manual Ex-Link TV control from the command line (frames from `tv.py`). Power and inputs work; the volume/mute subcommands are acked and refused on this rig (see Conventions). |
| `calibrate.py` | Rediscovers the controller's HID button bytes after a firmware change. |
| `haptic_test.py` | Bench tool for the controller's haptic output reports. Run only with the listener stopped. |
| `config.example.json` | Template for `config.json` (gitignored): MAC, IPs, COM port, TV input mapping, voice tuning. Every key is documented inline. |
| `secrets.template.json` | Template for `secrets.json` (gitignored): Deepgram, Anthropic, OpenAI, Steam, media-service, Langfuse, and Grafana keys. |
| `Start-K15.bat` | The Startup-folder target, and the one thing to run after a `git pull`. Per lane: supervisor down → start it; supervisor up → kill the *agent* so its supervisor relaunches it on new code. Never bounces a supervisor window, so a live session's watch loop survives. |
| `Start-Listener.bat` | Chord-lane supervisor: `reconcile` once, then the listener in a 10 s restart loop. Single-instance. |
| `alloy/config.alloy.example` | Grafana Alloy config template — ships `logs\k15-*.jsonl` to Loki. Copy to `config.alloy` (gitignored). |

### Voice overlay (`k15/voice/`)

Own venv, own pins. May import the chord lane's modules; never the reverse.

| File | Role |
|---|---|
| `voice_agent.py` | Composition root and wake loop. |
| `audio.py` | The PortAudio layer: device resolution, stream recovery, the wake listener. |
| `preroll.py` | Wake pre-roll buffer, so "hey jarvis volume up" as one sentence keeps its tail. |
| `session_runtime.py` | One session's Pipecat pipeline as a `Session` — Flux STT → grammar gate → dispatch, with the LLM assistant lane and cross-session carry. |
| `grammar_gate.py` | Deterministic intent matching as a Pipecat processor. Screens every final transcript before the LLM lane sees it. |
| `grammar.yaml` | The command grammar the gate matches against. |
| `titles.py` | Fuzzy game-title and collection resolution: Steam's strings ↔ what a human says. |
| `dispatch.py` | Every voice side effect — session, TV, launch, quit, Big Picture nav — plus the per-utterance snapshot. Shared by both lanes. |
| `tv_remote.py` | TV remote keys over WebSocket (port 8002) — the only volume-write path that works on this rig — and `TvDucker`, the session-length ducking built on them. Needs a one-time pairing, see below. |
| `assistant.py` | The catalog-in-context LLM lane: prompt, tool schemas and impls (store data, nav, quit, install), optional web search. |
| `assistant_repl.py` | The bench: each provider's plain SDK loop and the `--text` REPL over the same prompt and tools. |
| `operations.py` / `announce.py` | Durable correlation for Steam installs and media acquisition: restart-safe observation, a diagnostic CLI, and proactive spoken completion. |
| `media.py` | Structured Radarr/Sonarr lookup, preset resolution, submission, completion observation (with bounded retries once an indexer outage clears), stack diagnostics, and optional Proton-to-qBittorrent port synchronization. Prowlarr/qBittorrent stay outside normal acquisition. |
| `text_interface.py` | Authenticated LAN chat endpoint over the same assistant tools and durable operations as voice. |
| `steam_session.py` | Optional signed-in Steam account session: install-by-voice, download status, and operation observation over ClientComm. Token-gated. |
| `earcons.py` | Earcon synthesis from specs at import — no binary audio assets in the repo. |
| `tracing.py` / `traces.py` / `llm_audit.py` | Langfuse spans, per-conversation JSON dumps under `state/traces/`, and a record of provider-executed tool calls. |
| `requirements.txt` / `constraints.txt` | Pinned deps, plus the as-built transitive versions passed to pip as `-c`. A constraints-only change must ride a `requirements.txt` touch or the dependency gate won't fire. |
| `models/` | Vendored wake models. `config.json`'s `wakeModel` selects one; the default is stock `hey_jarvis_v0.1`. |
| `bench/` | Hand-run probes: room recording and slicing, STT measurement, wake-model comparison, and verifier training. |
| `tests/` | The blind suite — the tests that need neither machine nor hardware. See Tests below. |
| `Start-Voice.bat` | Voice-lane supervisor, launched by `Start-K15.bat`: creates the venv on first run, then supervises the agent (single-instance, 10 s crash restart). The dependency gate lives inside the restart loop, so a `git pull` that changes pins installs them on the next agent launch. Args pass through — `Start-Voice.bat --dry-run` logs side effects instead of executing them. |

### Media sidecars (`k15/media/`)

The optional always-on FlareSolverr, Prowlarr, Radarr, and Sonarr Compose stack
shares one `/data` mount. FlareSolverr is internal-only; native qBittorrent
stays behind the Proton VPN split tunnel. The media root maps to `C:\Media`
initially and can move to a NAS without changing Slopstation records.
Provisioning and live validation are in `k15/media/README.md`.

### Wake-word training (`wake-training/`)

Gaming PC only; needs the GPU. The venv and ~16 GB of data live outside the
repo — data under `--root` (default `C:\Users\tillm\wake`), the venv wherever
`WAKE_VENV` points.

| File | Role |
|---|---|
| `pipeline.py` | Config → vendored model in one command. `Validate.bat` and `Train.bat` are wrappers around it. |
| `alfred.yaml` | The training recipe. |
| `Validate.bat` | Build the threshold optimiser's validation set from held-out room audio. Run before `Train.bat`. |
| `Train.bat` | Train every size, or one. |
| `Bench.bat` / `bench_real.py` | Rank candidates on real room audio through openWakeWord, against the vendored incumbents. This is the eval that picks a model. |
| `make_validation.py` | Featurizes the held-out audio `Validate.bat` points at. |

## Running it

Run `.bat` files by double-clicking, or from PowerShell with a leading `.\`
(`.\Start-K15.bat`) — PowerShell will not run anything from the current
directory without an explicit path.

**Deploy.** K15: `git pull`, then `Start-K15.bat`. Gaming PC: `Deploy.ps1` from
a checkout on the PC — never hand-copy.

**Diagnose.** On the K15, `python doctor.py`. On the PC:

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Doctor.ps1
```

The box is `Restricted`, so `-ExecutionPolicy Bypass` is required — every
scheduled task and the sshd forced command invoke PowerShell the same way. Both
doctors are read-only and exit with their FAIL count.

From `k15\voice`, inspect long-running work with
`.venv\Scripts\python operations.py list`; `show <id>`, `reconcile`, and
`cancel <id>` are the other diagnostic commands. Media operations also support
`abandon <id> --execute`, which cleans up in Radarr or Sonarr before recording
the cancellation.

**Text.** Put 32 random bytes in `secrets.json` as `textInterfaceToken` and add the
`textInterface` block from `config.example.json` to `config.json`. `python
k15\slop.py` then opens a general chat over the same tools as voice; on the K15 it
reads the local config and token. To reach it from the gaming PC, set that block's
`host` to `0.0.0.0`, allow its port inbound on the Private profile only
(`New-NetFirewallRule -Profile Private -RemoteAddress LocalSubnet`), and set
`SLOPSTATION_URL` and `SLOPSTATION_TOKEN` in that shell.

**Correlate.** Every intent carries a `turn` id from the wake word or the chord
through to the gaming PC. It appears in `k15/logs/k15-YYYYMMDD.jsonl`, in
`C:\CouchGaming\logs\pc-YYYYMMDD.jsonl`, and in the PC transcript filename:

```bash
grep '"turn":"9f2c1a"' k15/logs/k15-*.jsonl
```

## Tests

The suite runs as plain scripts, not under pytest — `events._env()` detects
tests by `sys.argv[0]`, so pytest would label their events `env=prod`. From
`k15\voice\`:

```
.venv\Scripts\python tests\run.py
```

runs every file in its own process and skips what the machine lacks:
`test_library` needs a local Steam (detected), `test_session_pipeline` and
test_preroll's pipeline-ordering check real audio devices
(`set CG_TEST_AUDIO=1`, the K15); `--all` forces both. Every test
that touches the K15 modules starts with `import _bootstrap` (paths, a temp log
dir and state dir, a config fixture, so the suite runs on a checkout with no
`config.json`); `test_ps_parse` is stdlib-only and does not.

Four tests are the repo's rules: `test_event_names` freezes the event
vocabulary, field keys and lanes (a rename is a deliberate edit there);
`test_imports` imports every module with no config and no hardware and checks
every `module.attr` resolves; `test_lint` is pyflakes plus the lane rule read
off the AST; `test_ps_parse` parses every `.ps1` and checks the marker paths,
charsets and turn order agree between `Dispatch.ps1` and `common.ps1`.
`test_turn` reads the shipping `Dispatch.ps1`, so gaming-pc regex changes are
drilled from here.

Every push runs this suite plus mypy (`mypy.ini`, lenient: it holds the
existing annotations consistent, it does not demand new ones) on a Windows
runner — see `.github/workflows/ci.yml`.

## Configuration

Per-machine files are gitignored and created once from committed templates, so a
checkout runs without local config fighting `git pull`:

- `k15/config.json` ← `k15/config.example.json`
- `k15/secrets.json` ← `k15/secrets.template.json`
- `k15/alloy/config.alloy` and `gaming-pc/alloy/config.alloy` ← the `.example`
  beside each

`config.example.json` documents every key inline. Two worth knowing about:

- **`tvIp`** is optional but gates two features. Without it the launch path
  falls back to a blind `power_on`, and TV ducking stays off.
- **`duckSteps`** / **`duckToPct`** drop the soundbar for the length of a voice
  session. They need `tvIp` plus a one-time WebSocket pairing, run from the K15
  and accepted on the TV:

  ```
  .venv\Scripts\python tv_remote.py pair
  ```

## Telemetry

Events land as JSONL beside `couch.log` on each machine (on the PC, the task
scripts write `pc-*.jsonl` and `Dispatch.ps1` writes `pc-dispatch-*.jsonl`).
Grafana Alloy runs as a Windows service per machine and ships them to Loki;
`doctor.py` checks that it is installed and running. Agent traces go to
Langfuse.

The local JSONL is the source of truth and Grafana is a mirror — Alloy's
position file tracks what it read, not what it sent, so lines read during an
outage are dropped rather than queued.

Two skills in [`.claude/skills/`](.claude/skills/) query this from an agent
session: `grafana-logs` (launches, errors, liveness, both machines) and
`langfuse-traces` (what the assistant heard, said, and cost). Each carries a
stdlib-only query script.

## Conventions

| Item | Value |
|---|---|
| Gaming PC | `TILLMAN-DESKTOP` · `192.168.68.67` · MAC `74-56-3C-45-92-DD` · user `tillm` |
| K15 | `K15` · `192.168.68.75` · user `minipc` |
| Puck | The controller's wireless receiver, shared over VirtualHere as `K15.5` — VID `28DE`, PID `1304` |
| Ex-Link serial | `COM3` on the K15 · 9600 8N1 |
| TV | Samsung S90C · EDID name `QCQ90S` · `192.168.68.51` |
| TV inputs | HDMI1 Apple TV · HDMI2 PS5 · HDMI3 eARC · HDMI4 PC |
| Audio | eARC soundbar (HW-Q990C). The TV refuses direct volume writes — Ex-Link acks then shows "Not Available", UPnP `SetVolume` answers 501. Only remote-key relay over WebSocket moves it. |
| Display topology | `Test-TvIsPrimary` reads "primary display height equals `2160`". Revisit before pairing this rig with a 4K or 5K2K (5120×2160) desk monitor. |
| Conflict rule | Teardown wins, launch queues, safety stands down. Exit stops a running Enter; Enter waits briefly for a running Exit then aborts; Office-Safety does nothing while either runs. |
| The one rule | Nothing switches the TV to HDMI 4 before the PC writes `READY`. |

**SSH surface** — eleven verbs, everything else answers `DENIED`. Every
state-changing verb also accepts a `--turn <hex>` suffix.

```
ssh gamepc enter | exit | status | enterstate | version
           games | playing | collections
           launch <appid> | stop <appid>
           nav downloads|library|store | nav details|store <appid> | nav collection <name>
```

## Not in the repo

- **VirtualHere binaries** — `vhui64.exe` (PC client) and `vhusbdwinw64.exe`
  (K15 server). Download from virtualhere.com.
- **VirtualHere `config.ini`** — contains the EasyFind ID and PIN. Never commit.
- **`OFFICE.lnk` / `TV-GAMING.lnk`** — machine-generated DisplayMagician profile
  shortcuts.
- **`config.json`, `secrets.json`, `config.alloy`** — per-machine, created from
  the committed templates.
- **Scheduled task registrations, sshd setup, firewall rules** — one-time
  commands run on the machines themselves.
- **Logs and runtime state** — `logs/`, `state/session.lock`, `couch.log`, and
  the voice `.venv` (created on-machine).
- **Wake-training data and venv** — ~16 GB under `--root`, plus `WAKE_VENV`.
