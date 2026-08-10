# slopstation

One-chord couch gaming console: RTX 4090 gaming PC → direct HDMI → Samsung S90C,
orchestrated by a GMKtec K15 mini PC. Hold Steam + right trigger on the Steam
Controller for 2 s and the K15 powers the TV on, wakes the PC, flips displays to
TV-only, claims the controller Puck over VirtualHere, launches Big Picture, and
only then switches the TV input — one visible transition, and any failure before
READY leaves the TV exactly where it was.

Full build guide, design rationale, and failure drills: [docs/couch-gaming-guide.md](docs/couch-gaming-guide.md)

## Layout

| Repo path | Deployed at | Machine |
|---|---|---|
| `gaming-pc/` | `C:\CouchGaming\` | `TILLMAN-DESKTOP` (gaming PC) |
| `k15/` | `C:\Users\minipc\Desktop\` | `K15` (orchestrator mini PC) |

This repo is the **source of truth / archive** — scripts are deployed by copying
to the paths above, not by cloning in place.

### Gaming PC (`gaming-pc/`)

| Script | Role |
|---|---|
| `Enter-TV.ps1` | Session setup: TV-GAMING profile, Puck claim, Big Picture, `READY` marker. Run as Scheduled Task `\CouchGaming\Enter`. |
| `Exit-TV.ps1` | Teardown: close Big Picture, restore OFFICE, release Puck, blank monitor. Task `\CouchGaming\Exit`. |
| `Office-Safety.ps1` | Unconditional OFFICE restore at every logon. Task `\CouchGaming\ForceOfficeAtLogon`. |
| `Wake-Safety.ps1` | Cleans up sessions abandoned before sleep; distinguishes keyboard vs WoL wakes. Task `\CouchGaming\WakeSafety`. |
| `Dispatch.ps1` | Entire SSH attack surface: `enter` / `exit` / `status`, everything else `DENIED`. Forced command in `administrators_authorized_keys`. |

### K15 (`k15/`)

| Script | Role |
|---|---|
| `couch.py` | Orchestrator: Ex-Link TV power → WoL → `ssh enter` → poll READY → switch input → watch loop. |
| `chord_listener.py` | Watches the Puck's HID stream for Steam + right-trigger held 2 s; fires `couch.py start`. |
| `exlink.py` | Manual Ex-Link TV control (`python exlink.py power_on\|power_off\|hdmi1..4`). |
| `calibrate.py` | Rediscovers the controller's HID button bytes after firmware changes. |
| `config.json` | Orchestrator config (MAC, IPs, COM port, input mapping). |
| `Start-TV-Gaming.bat` | Desktop recovery launcher for `couch.py`. |
| `Start-Listener.bat` | Startup-folder shortcut target that runs the chord listener. |

## Deliberately not in the repo

- **VirtualHere binaries** (`vhui64.exe` client on the PC, `vhusbdwinw64.exe` server on the K15) — download from virtualhere.com.
- **VirtualHere `config.ini`** — contains the EasyFind ID/PIN, which are remote-access credentials. Never commit.
- **`OFFICE.lnk` / `TV-GAMING.lnk`** — machine-generated DisplayMagician profile shortcuts; recreate per guide Stage 6.
- **Scheduled task registrations, sshd setup, firewall rules** — one-time commands, all in the guide (Stages 6–8).
- **Logs and runtime state** (`logs/`, `state/session.lock`, `couch.log`).

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
