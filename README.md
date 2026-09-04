# slopstation

[![ci](https://github.com/trillville/slopstation/actions/workflows/ci.yml/badge.svg)](https://github.com/trillville/slopstation/actions/workflows/ci.yml)
[![cd](https://github.com/trillville/slopstation/actions/workflows/cd.yml/badge.svg)](https://github.com/trillville/slopstation/actions/workflows/cd.yml)

Slopstation turns a Windows gaming PC, Samsung S90C, and Steam Controller into
a couch console. A GMKtec K15 mini PC controls the TV and gaming PC, accepts
voice and text commands, and tracks Steam and media downloads.

## How it works

A controller chord starts a session:

1. The K15 powers on the TV, switches it to HDMI 4, and wakes the gaming PC.
2. The gaming PC switches to its TV display profile, claims the controller,
   and opens Steam Big Picture.
3. The gaming PC returns `READY` with the request ID.
4. The K15 confirms the TV input.
5. Slopstation watches the session until it ends.
6. The gaming PC restores the office display and releases the controller.

A failed launch that woke the TV turns it off. Controller input and voice run
in separate processes, so either can restart independently.

The assistant is available through voice, a local text client, and an optional
MCP endpoint. Fixed commands are handled locally; other requests use the same
assistant tools on every interface. Steam, Radarr, and Sonarr perform
long-running work while Slopstation records progress in
`state/operations.json`.

## Repository layout

| Path | Purpose |
|---|---|
| `src/slopstation/couch.py` | Start, watch, and end couch sessions |
| `src/slopstation/chord_listener.py` | Listen for the controller chord |
| `src/slopstation/tv.py`, `haptics.py`, `gamepc.py` | TV, controller, and gaming-PC access |
| `src/slopstation/agent/speech/` | Wake word, audio, and fixed voice commands |
| `src/slopstation/agent/brain/` | Assistant prompts, providers, and tools |
| `src/slopstation/agent/tools/` | Steam, media, operation, and TV tools |
| `src/slopstation/agent/interfaces/` | Text and MCP interfaces |
| `gaming-pc/` | Gaming-PC scripts and SSH command allowlist |
| `media/` | Optional media stack and its [setup guide](media/README.md) |
| `tests/` | Python and PowerShell tests |

The custom wake model comes from
[slopstation-voice-lab](https://github.com/trillville/slopstation-voice-lab).

## Install the K15

1. Clone the repository to `C:\Users\minipc\Desktop\slopstation`.
2. Copy `config.example.json` to `config.json` and
   `secrets.template.json` to `secrets.json`, then fill in device names,
   addresses, API keys, and tokens. Both destination files are ignored by Git.
3. Create the virtual environment and install the package:

   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e ".[dev]" -c constraints.txt
   ```

4. Install VirtualHere Server, reserve the K15's address in DHCP, and allow its
   port on the private LAN:

   ```powershell
   New-NetFirewallRule -DisplayName 'VirtualHere USB hub (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7575 -Profile Private -RemoteAddress LocalSubnet
   ```

5. Connect the TV's Ex-Link adapter and set its COM port in `config.json`.
6. Register and start the controller and voice tasks from a non-administrator
   PowerShell window:

   ```powershell
   .\Setup-K15-Tasks.ps1
   .\Start-Slopstation.bat
   ```

7. If volume ducking is enabled, pair the TV remote and accept the TV prompt:

   ```powershell
   .venv\Scripts\python -m slopstation.agent.tools.tv_remote pair
   ```

8. Run `.venv\Scripts\slopstation-doctor` until no checks fail.

### Optional MCP access

MCP forwards requests to the existing text interface.

1. Set `textInterfaceToken` and `remoteInterfaceToken` in `secrets.json`.
   Each token should contain at least 32 random bytes.
2. Enable `textInterface` and `remoteInterface` in `config.json`. Keep the
   remote interface on `127.0.0.1:8766`.
3. Route a Cloudflare named tunnel to `http://127.0.0.1:8766` and restrict the
   public hostname to the connector's documented source addresses.
4. Add a custom connector for `https://<host>/mcp` with
   `Authorization: Bearer <remoteInterfaceToken>`.
5. Restart Slopstation and run the doctor.

The MCP endpoint holds no assistant state and does not expose a separate set
of tools.

## Install the gaming PC

1. Install Steam, DisplayMagician, VirtualHere Client, and Windows OpenSSH
   Server.
2. Create working `OFFICE.lnk` and `TV-GAMING.lnk` DisplayMagician profiles.
3. Configure the `CouchGaming` scheduled tasks and set `Dispatch.ps1` as the
   forced command for the K15's key in
   `C:\ProgramData\ssh\administrators_authorized_keys`.
4. Deploy from a repository checkout:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\gaming-pc\Deploy.ps1
   ```

5. Run the deployed doctor:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Doctor.ps1
   ```

For Radarr, Sonarr, and qBittorrent setup, see
[media/README.md](media/README.md).

## Deployment

A successful `ci` run on `main` starts `cd`. The K15 deploys immediately; the
gaming-PC job waits until that machine wakes. Both machines use self-hosted
runners because they do not accept inbound deployment connections.

Deployments wait for active couch sessions to finish. They fail instead of
interrupting a session or rolling back automatically. Because this repository
is public, `cd` only deploys commits pushed to `main`; pull-request code never
runs on either machine.

Register repository runners with the labels `k15` and `gamepc`. Run them in the
logged-in desktop session, not as services. The K15 runner executes the live
checkout directly; set the `K15_CHECKOUT` repository variable if it is not at
the default path above.

To update the K15 manually:

```powershell
git pull
.\Start-Slopstation.bat
.venv\Scripts\slopstation-doctor
```

## Common commands

```powershell
# Text interface
.venv\Scripts\python -m slopstation.slop
.venv\Scripts\python -m slopstation.slop "what is downloading?"

# Tracked Steam and media work
.venv\Scripts\python -m slopstation.agent.tools.operations list --active
.venv\Scripts\python -m slopstation.agent.tools.operations show <operation-id>
.venv\Scripts\python -m slopstation.agent.tools.operations reconcile
```

`operations abandon <operation-id> --execute` removes a tracked media request
through Radarr or Sonarr.

For LAN text access, bind the text endpoint to `0.0.0.0`, restrict its firewall
rule to `LocalSubnet` on the Private profile, and set `SLOPSTATION_URL` and
`SLOPSTATION_TOKEN` on each client. MCP remains on localhost behind its tunnel.

## Telemetry

Structured events are written to:

- K15: `logs\k15-YYYYMMDD.jsonl`
- Gaming-PC tasks: `C:\CouchGaming\logs\pc-YYYYMMDD.jsonl`
- Gaming-PC SSH dispatcher: `C:\CouchGaming\logs\pc-dispatch-YYYYMMDD.jsonl`

The OpenTelemetry Collector configurations in `otelcol/config.yaml.example`
and `gaming-pc/otelcol/config.yaml.example` send those logs to Sentry. The
voice process also sends errors and traces when `sentryDsn` is configured.

## Development

Run the same checks as CI:

```powershell
.venv\Scripts\pytest
.venv\Scripts\ruff check .
.venv\Scripts\mypy
```

Tests that require a local Steam installation skip when it is absent. Tests
that open real audio devices require `SLOPSTATION_TEST_AUDIO=1`.

## Hardware assumptions

| Component | Value |
|---|---|
| Gaming PC | `TILLMAN-DESKTOP`, `192.168.68.67`, user `tillm` |
| K15 | `K15`, `192.168.68.75`, user `minipc` |
| TV | Samsung S90C, gaming PC on HDMI 4, Ex-Link on K15 `COM3` |
| Controller | VirtualHere device `Steam Controller Puck`, VID `28DE`, PID `1304` |
| Audio | HW-Q990C over eARC |
| TV detection | Primary display height is `2160` |

Revisit the display check before adding another 2160-pixel-high monitor.

Runtime configuration, secrets, media state, VirtualHere files,
DisplayMagician shortcuts, scheduled tasks, logs, and the virtual environment
are not stored in Git.
