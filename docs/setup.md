# Setup

Two machines. The mini PC is always on; it runs Slopstation, talks to the
TV, and wakes the gaming PC. The code calls it the K15, after the GMKtec K15
ours runs on, so that name appears in task names, the runner label and log
file names. The gaming PC runs Steam and a small set of PowerShell scripts
under `C:\CouchGaming`.

Every value you set is listed in [configuration.md](configuration.md).
Running the system day to day is [operations.md](operations.md). The optional
media stack has its own guide, [media/README.md](../media/README.md).

## Mini PC

Prerequisites: Windows 11, Python 3.13, Git, the TV's Ex-Link serial adapter,
and for voice a USB microphone and a speaker. The TV must be a Samsung set
with an Ex-Link port: power and input go over that serial link, volume ducking
uses UPnP over HTTP, and remote keys use Samsung's WebSocket API. Another make means
replacing `tv.py` and `agent/tools/tv_remote.py`.

1. Clone the repository, for example to `C:\slopstation`. The checkout is the
   installation: `config.json`, `secrets.json`, `state\` and `logs\` sit
   beside the code and are ignored by Git.
2. Copy `config.example.json` to `config.json` and `secrets.example.json` to
   `secrets.json`, then fill in device names, addresses, API keys and tokens.
3. Create the virtual environment and install the package with the frozen
   constraints:

   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e ".[dev]" -c constraints.txt
   ```

4. Install VirtualHere Server, plug the controller's receiver into the mini PC,
   reserve the mini PC's address in DHCP, and allow the server's port on the
   private LAN:

   ```powershell
   New-NetFirewallRule -DisplayName 'VirtualHere USB hub (LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7575 -Profile Private -RemoteAddress LocalSubnet
   ```

5. Connect the Ex-Link adapter and set its COM port as `tvComPort`.
6. Create an SSH key for the mini PC's user and an entry in that user's
   `.ssh\config` named as `sshHost` in `config.json`, with the gaming PC's
   address, user and key. The gaming-PC installer below binds the public key
   to the dispatcher.
7. Register the two lane tasks and start them. Use a non-administrator
   PowerShell window: a lane started elevated cannot be stopped by the
   deployer.

   ```powershell
   .\Setup-K15-Tasks.ps1
   .\Start-Slopstation.bat
   ```

8. Set `tvIp` for volume commands and ducking; these do not require pairing.
   For mute and manual remote keys, pair with the TV and accept its prompt:

   ```powershell
   .venv\Scripts\python -m slopstation.agent.tools.tv_remote pair
   ```

9. Run the doctor until it ends with `0 fail`:

   ```powershell
   .venv\Scripts\slopstation-doctor
   ```

### Optional: text access from the LAN

Set `textInterfaceToken` in `secrets.json`, enable `textInterface` in
`config.json` with host `0.0.0.0`, and allow its port on the Private profile
for `LocalSubnet` only. On each client set `SLOPSTATION_URL` and
`SLOPSTATION_TOKEN`, then use `slopstation-text`.

### Optional: MCP access from outside the LAN

The MCP endpoint forwards to the text interface and holds no state of its own.

1. Set `textInterfaceToken` and `remoteInterfaceToken` in `secrets.json`, at
   least 32 random bytes each.
2. Enable `textInterface` and `remoteInterface` in `config.json`. Keep the
   remote interface on `127.0.0.1:8766`.
3. Route a Cloudflare named tunnel to `http://127.0.0.1:8766` and restrict the
   public hostname to the connector's documented source addresses.
4. Add a custom connector for `https://<host>/mcp` with
   `Authorization: Bearer <remoteInterfaceToken>`.
5. Restart Slopstation and run the doctor.

### Optional: log shipping to Sentry

Set `sentryDsn` in `config.json` for the voice agent's errors, traces and
check-ins. For the event logs, install the OpenTelemetry Collector contrib
build, copy `otelcol\config.yaml.example` to the service's configuration path
(the file's header says how to find it), set the log directory and the Sentry
endpoint, and restart the service. The gaming PC's copy is
`gaming-pc\otelcol\config.yaml.example`.

## Gaming PC

Prerequisites: Windows 11, wired Ethernet with Wake-on-LAN enabled in the NIC
driver, Steam, DisplayMagician, VirtualHere Client, and Windows OpenSSH
Server. `Doctor.ps1` checks the three NIC wake settings a launch depends on.

1. Create two DisplayMagician profiles, one for the desk and one with the TV
   as the only display, and save their shortcuts as `C:\CouchGaming\OFFICE.lnk`
   and `C:\CouchGaming\TV-GAMING.lnk`.
2. Put the VirtualHere client at `C:\CouchGaming\vhui64.exe`, point it at the
   mini PC's server, and have it start at logon.
3. From an elevated PowerShell in a repository checkout, as the user who sits
   at the desktop:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\gaming-pc\Install.ps1 -K15Address <mini PC address> -K15PublicKey '<the mini PC public key line>'
   ```

   The first run creates `C:\CouchGaming\config.psd1` from
   `gaming-pc\config.example.psd1` and stops so you can check the values.
   The second run deploys the scripts, registers the seven `CouchGaming`
   scheduled tasks, allows SSH from the mini PC only, binds the mini PC's key to
   `Dispatch.ps1`, and ends with the doctor. Re-run it whenever a task or the
   rule needs correcting.

4. From the mini PC, `ssh <sshHost> status` should answer `NOTREADY`. The mini PC
   doctor's `ssh dispatch` row checks the same thing.

## Continuous deployment

Both machines run self-hosted GitHub Actions runners, because neither accepts
inbound connections. Register them with the labels `k15` and `gamepc`, and run
them in the logged-in desktop session rather than as services so they can
reach the same tasks and devices the lanes use. Set the repository variable
`K15_CHECKOUT` to the live checkout's path. What CD does after that is in
[operations.md](operations.md).
