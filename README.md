# slopstation

One-chord couch gaming console: RTX 4090 gaming PC → direct HDMI → Samsung S90C,
orchestrated by a GMKtec K15 mini PC. Hold Steam + right trigger on the Steam
Controller for 2 s and the K15 powers the TV on, wakes the PC, flips displays to
TV-only, claims the controller Puck over VirtualHere, launches Big Picture, and
only then switches the TV input — one visible transition, and any failure before
READY leaves the TV exactly where it was.

Build narrative, rationale, registration commands, and failure drills:
[docs/couch-gaming-guide.md](docs/couch-gaming-guide.md) — a **historical record
of the v4 system** (frozen at v4.4). The scripts here are canonical; guide code
listings are no longer maintained in lockstep.

## Layout

| Repo path | Runs at | Machine | How |
|---|---|---|---|
| `gaming-pc/` | `C:\CouchGaming\` | `TILLMAN-DESKTOP` (gaming PC) | copy (needs gitignored `.lnk`/`.exe`) |
| `k15/` | a clone on the Desktop | `K15` (orchestrator mini PC) | `git pull` in place |

Every script derives its sibling paths from its own location, so a folder is a
relocatable unit and runs fine straight from a checkout. The **K15 runs from a
clone** (`git pull` to update — no copying). The **gaming PC deploys by copy**,
because its runtime needs gitignored binaries/shortcuts (`vhui64.exe`,
`OFFICE.lnk`, `TV-GAMING.lnk`) that can't live in the repo.

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
| `Dispatch.ps1` | Entire SSH attack surface: `enter` / `exit` / `status`, everything else `DENIED`. Forced command in `administrators_authorized_keys`; deliberately dependency-free. |
| `Doctor.ps1` | On-demand chain diagnosis: files, tasks, sshd/firewall/key ACL, VirtualHere, display probe, session state. Read-only; exit code = FAIL count. |

### K15 (`k15/`)

| File | Role |
|---|---|
| `cglib.py` | Shared module: Ex-Link frame table, Puck VID/PID, config loading, tagged logging to `couch.log`. |
| `couch.py` | Orchestrator: Ex-Link TV power → WoL → `ssh enter` → poll READY → switch input → watch loop. `reconcile` subcommand re-adopts or clears a session lock that survived a K15 restart. |
| `chord_listener.py` | Watches the Puck's HID stream for Steam + right-trigger held 2 s and answers through the controller — 1 thud = launching, 2 = busy, 3 = the launch failed — then fires `couch.py start`. Logs to `couch.log` as `[listener]`. |
| `haptic_test.py` | Bench tool for the controller's haptic output reports (chirp/pulse/rumble variants). Run only with the listener stopped; re-run after firmware updates. |
| `doctor.py` | On-demand chain diagnosis: config, deps, Ex-Link port, Puck, listener, haptics (auto-skipped while the listener owns the Puck), ssh contract, session state. |
| `exlink.py` | Manual Ex-Link TV control (`python exlink.py power_on\|power_off\|hdmi1..4`); COM port from config. |
| `calibrate.py` | Rediscovers the controller's HID button bytes after firmware changes. |
| `config.json` | Orchestrator config (MAC, IPs, COM port, input mapping). |
| `Start-TV-Gaming.bat` | Desktop recovery launcher for `couch.py`. |
| `Start-Listener.bat` | Startup-folder shortcut target: runs `reconcile`, then the chord listener. |

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

## Deliberately not in the repo

- **VirtualHere binaries** (`vhui64.exe` client on the PC, `vhusbdwinw64.exe` server on the K15) — download from virtualhere.com.
- **VirtualHere `config.ini`** — contains the EasyFind ID/PIN, which are remote-access credentials. Never commit.
- **`OFFICE.lnk` / `TV-GAMING.lnk`** — machine-generated DisplayMagician profile shortcuts; recreate per guide Stage 6.
- **`k15/config.json`, `k15/secrets.json`** — per-machine config and API keys; create once from `config.example.json` / `secrets.template.json`. Gitignored so a checkout runs without them fighting `git pull`.
- **Scheduled task registrations, sshd setup, firewall rules** — one-time commands, all in the guide (Stages 6–8).
- **Logs and runtime state** (`logs/`, `state/session.lock`, `couch.log`), and the voice `.venv` (created on-machine).

## Conventions

| Item | Value |
|---|---|
| Gaming PC | `TILLMAN-DESKTOP` · `192.168.68.67` · MAC `74-56-3C-45-92-DD` · user `tillm` |
| K15 | `K15` · `192.168.68.75` · user `minipc` |
| Puck (VirtualHere) | `K15.5` — VID `28DE`, PID `1304` |
| Ex-Link serial | `COM3` on the K15 · 9600 8N1 |
| TV inputs | HDMI1 Apple TV · HDMI2 PS5 · HDMI3 eARC · HDMI4 PC |
| TV EDID name | `QCQ90S` |
| Remote surface | `ssh gamepc enter\|exit\|status` — nothing else exists |
| The one rule | Nothing switches the TV to HDMI 4 before the host writes `READY` |
