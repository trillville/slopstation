# slopstation

[![ci](https://github.com/trillville/slopstation/actions/workflows/ci.yml/badge.svg)](https://github.com/trillville/slopstation/actions/workflows/ci.yml)
[![cd](https://github.com/trillville/slopstation/actions/workflows/cd.yml/badge.svg)](https://github.com/trillville/slopstation/actions/workflows/cd.yml)

Slopstation turns a Windows gaming PC, a Samsung TV, and a Steam Controller
into a couch console. An always-on mini PC controls the TV and the gaming PC,
takes voice and text commands, and tracks Steam and media downloads. Press a
controller chord on the couch and the TV comes on, the PC wakes, and Steam Big
Picture is on screen with the controller working. Say "hey Alfred, play Hades"
and it launches.

It is one household's system, run every day and deployed from this
repository. It is not a framework.

## How a session starts

```mermaid
flowchart LR
    chord[Controller chord] --> mini[Mini PC]
    voice[Voice and text] --> mini
    mini -->|serial, WebSocket| tv[Samsung TV]
    mini -->|Wake-on-LAN, SSH, controller over USB/IP| pc[Gaming PC]
    pc -->|HDMI| tv
    mini --> media[Steam, Radarr, Sonarr]
```

1. The mini PC powers on the TV, switches it to the PC's input, and wakes the
   gaming PC.
2. Over SSH, the mini PC tells the gaming PC to enter. The PC switches to its TV
   display profile, claims the controller over USB-over-IP, and opens Steam
   Big Picture.
3. The gaming PC returns `READY` with the request's turn id.
4. The mini PC confirms the TV input and watches the session until it ends.
5. The gaming PC restores the office display and releases the controller.

A failed launch that woke the TV turns it off again. Controller input and
voice run as separate processes, so either can restart on its own.

## Reference hardware

| Component | Used here |
|---|---|
| Gaming PC | Windows 11, wired Ethernet with Wake-on-LAN, Steam, DisplayMagician, VirtualHere client, Windows OpenSSH server |
| Mini PC | GMKtec K15, Windows 11, always on. Runs Slopstation, the VirtualHere server, and the optional media stack. The code calls this machine the K15: in task names, the runner label, log file names and `K15_CHECKOUT` |
| TV | Samsung S90C. Gaming PC on HDMI 4, Ex-Link serial adapter on the mini PC |
| Controller | Steam Controller, its Puck receiver plugged into the mini PC and shared to the gaming PC over VirtualHere |
| Audio | Samsung HW-Q990C soundbar over eARC |
| Speech | openWakeWord, Deepgram for speech to text and text to speech, Anthropic or OpenAI for the assistant |

The TV is the one hard dependency on a brand. Power and input go over Ex-Link,
Samsung's serial control protocol, and the remote and volume ducking use
Samsung's WebSocket API, so another make of TV means replacing `tv.py` and
`agent/tools/tv_remote.py`.

The gaming PC's per-installation values (controller name and hardware id,
the TV's EDID name, the display height that identifies the TV) live in
`C:\CouchGaming\config.psd1`; see `gaming-pc/config.example.psd1`.

The custom wake model in `src/slopstation/agent/models` was trained by the
author on recordings from this room with
[slopstation-voice-lab](https://github.com/trillville/slopstation-voice-lab).
It is the author's own work and is covered by this repository's MIT license.
The stock `hey_jarvis_v0.1` model in `config.example.json` works without it.

## Evaluating without the hardware

Clone, create the virtual environment, and run the tests. Tests that need a
local Steam installation or real audio devices skip themselves.

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]" -c constraints.txt
.venv\Scripts\pytest
```

Then read `src/slopstation/couch.py` for the launch,
`gaming-pc/Dispatch.ps1` for the PC side, and
`src/slopstation/agent/dispatch.py` for the assistant's actions.

## Repository layout

| Path | Purpose |
|---|---|
| `src/slopstation/couch.py` | Start, watch, and end couch sessions |
| `src/slopstation/chord_listener.py` | Listen for the controller chord |
| `src/slopstation/text_client.py` | Send text commands from a terminal |
| `src/slopstation/tv.py`, `haptics.py`, `gamepc.py` | TV, controller, and gaming-PC access |
| `src/slopstation/agent/voice.py` | Run the voice service |
| `src/slopstation/agent/speech/` | Wake word, audio, and fixed voice commands |
| `src/slopstation/agent/llm/` | Assistant prompts and model providers |
| `src/slopstation/agent/dispatch.py` | Actions shared by voice and text commands |
| `src/slopstation/agent/tools/` | Steam, media, operation, and TV tools |
| `src/slopstation/agent/interfaces/` | Text and MCP interfaces |
| `gaming-pc/` | Gaming-PC scripts and the SSH command allowlist |
| `media/` | Optional media stack and its [setup guide](media/README.md) |
| `tests/` | Python and PowerShell tests |

## Install the mini PC

1. Clone the repository, for example to `C:\slopstation`.
2. Copy `config.example.json` to `config.json` and
   `secrets.example.json` to `secrets.json`, then fill in device names,
   addresses, API keys, and tokens. Both destination files are ignored by Git.
3. Create the virtual environment and install the package:

   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e ".[dev]" -c constraints.txt
   ```

4. Install VirtualHere Server, reserve the mini PC's address in DHCP, and allow its
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
2. Create working `OFFICE.lnk` and `TV-GAMING.lnk` DisplayMagician profiles
   in `C:\CouchGaming`, and put `vhui64.exe` there.
3. From an elevated PowerShell in a repository checkout, as the user who sits
   at the desktop:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\gaming-pc\Install.ps1 -K15Address <mini PC address> -K15PublicKey '<the mini PC public key line>'
   ```

   The first run creates `C:\CouchGaming\config.psd1` from
   `gaming-pc\config.example.psd1` and stops so you can check the values.
   The second run deploys the scripts, registers the seven `CouchGaming`
   scheduled tasks, allows SSH from the mini PC only, binds the mini PC's key to
   `Dispatch.ps1`, and ends with the doctor. It can be re-run at any time.

4. Later deploys need only `gaming-pc\Deploy.ps1` from a checkout, or CD.
   `C:\CouchGaming\Doctor.ps1` reports the state at any time.

For Radarr, Sonarr, and qBittorrent setup, see
[media/README.md](media/README.md).

## Deployment

A successful `ci` run on `main` starts `cd`. The mini PC deploys immediately; the
gaming-PC job waits until that machine wakes. Both machines use self-hosted
runners because they do not accept inbound deployment connections.

Deployments wait for active couch sessions to finish. They fail instead of
interrupting a session or rolling back automatically. Because this repository
is public, `cd` only deploys commits pushed to `main`; pull-request code never
runs on either machine.

Register repository runners with the labels `k15` and `gamepc`. Run them in the
logged-in desktop session, not as services. The mini PC runner executes the live
checkout directly; set the `K15_CHECKOUT` repository variable to that
checkout's path.

To update the mini PC manually:

```powershell
git pull
.\Start-Slopstation.bat
.venv\Scripts\slopstation-doctor
```

## Common commands

```powershell
# Text interface
.venv\Scripts\slopstation-text
.venv\Scripts\slopstation-text "what is downloading?"

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

- Mini PC: `logs\k15-YYYYMMDD.jsonl`
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

Runtime configuration, secrets, media state, VirtualHere files,
DisplayMagician shortcuts, scheduled tasks, logs, and the virtual environment
are not stored in Git.

## License

MIT. See [LICENSE](LICENSE).
