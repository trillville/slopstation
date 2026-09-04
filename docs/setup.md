# Setup

Slopstation has two Windows installations. The always-on mini PC owns the
controller listener, TV control, assistant, and session state. The gaming PC
runs the interactive display and Steam scripts.

Use stable checkout paths. Local configuration, credentials, generated
shortcuts, third-party binaries, state, and logs are intentionally not stored
in Git.

## Always-on mini PC

### Prerequisites

Install:

- Python 3.13
- Git
- VirtualHere Server
- the driver for the TV's Ex-Link serial adapter
- Docker Desktop only when using the optional media stack

Reserve the mini PC and gaming PC addresses in DHCP. Connect the controller
receiver and Ex-Link adapter to the mini PC.

### Repository and Python

```powershell
git clone https://github.com/trillville/slopstation.git C:\slopstation
Set-Location C:\slopstation
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]" -c constraints.txt
Copy-Item config.example.json config.json
Copy-Item secrets.example.json secrets.json
```

Replace the documentation MAC and Internet Protocol (IP) addresses in
`config.json`. Fill only the credentials for features you intend to enable in
`secrets.json`. See [configuration.md](configuration.md) for every setting.

Allow the VirtualHere server from the private local network in an administrator
PowerShell:

```powershell
New-NetFirewallRule -DisplayName 'VirtualHere USB hub (LAN)' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7575 `
    -Profile Private -RemoteAddress LocalSubnet
```

### Startup tasks

Register both long-running lanes from a non-administrator PowerShell. They must
run in the signed-in user session so audio and controller devices are visible.

```powershell
Set-Location C:\slopstation
.\Setup-K15-Tasks.ps1
.\Start-Slopstation.bat
```

If TV volume ducking is enabled, pair the network remote and accept the prompt
shown by the TV:

```powershell
.venv\Scripts\python -m slopstation.agent.tools.tv_remote pair
```

Validate the installation:

```powershell
.venv\Scripts\slopstation-doctor
```

Resolve every failure before relying on unattended startup. Warnings identify
optional or currently inactive features.

## Gaming PC

### Prerequisites

Install:

- Steam
- DisplayMagician
- VirtualHere Client
- Windows OpenSSH Server

Create `C:\CouchGaming`. Put the VirtualHere command-line client at
`C:\CouchGaming\vhui64.exe`. Configure it to find the mini PC's VirtualHere
server without auto-claiming devices.

In DisplayMagician, create and test two permanent-switch shortcuts:

- `C:\CouchGaming\OFFICE.lnk` restores the normal desk display.
- `C:\CouchGaming\TV-GAMING.lnk` makes the TV the primary gaming display.

Apply each shortcut repeatedly by hand before installing automation.

Install Windows OpenSSH Server when it is not already present:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

The installer starts `sshd`, makes it automatic, restricts TCP port 22 to the
supplied mini PC address, and disables the two standard broad OpenSSH firewall
rules when present.

### Create the automation key

On the mini PC:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\slopstation"
```

Do not add a passphrase; this key is used by unattended automation. The gaming
PC installer restricts it to `Dispatch.ps1` and disables port forwarding,
agent forwarding, X11 forwarding, and terminal allocation.

### Deploy and create local configuration

From an administrator PowerShell in a repository checkout on the gaming PC:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\gaming-pc\Install.ps1 `
    -K15Address '<mini-PC-IP>' `
    -K15PublicKeyPath '<path-to-slopstation.pub>'
```

The first run copies the scripts, creates
`C:\CouchGaming\config.psd1` from its example, and exits with code 2. Edit these
four values:

- `PuckName`: the controller receiver's VirtualHere device name.
- `PuckHwId`: the identifying portion of its Windows Plug and Play instance ID.
- `TvEdid`: the TV name returned by `WmiMonitorID`.
- `TvHeight`: the TV-primary desktop height.

List connected monitor names with:

```powershell
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID |
    ForEach-Object {
        -join [char[]]($_.UserFriendlyName | Where-Object { $_ -ne 0 })
    }
```

List the controller instance after claiming it in VirtualHere:

```powershell
Get-PnpDevice | Where-Object InstanceId -match 'VID_'
```

Rerun the installer with the same arguments. It creates or updates seven
`\CouchGaming\` tasks:

- `Enter`
- `Exit`
- `ForceOfficeAtLogon`
- `WakeSafety`
- `LaunchGame`
- `Nav`
- `StopGame`

The five on-demand tasks run without elevation in the interactive user
session. `ForceOfficeAtLogon` runs elevated 20 seconds after sign-in.
`WakeSafety` runs after the Windows power-resume event. Every task has a
five-minute execution limit so a wedged instance cannot block future runs.

When a public key path is supplied, the installer makes a one-line
`administrators_authorized_keys` file. If it replaces existing content, the
original is retained once as
`administrators_authorized_keys.before-slopstation`.

### Configure the mini PC SSH alias

Add an entry to `%USERPROFILE%\.ssh\config` on the mini PC:

```sshconfig
Host gaming-pc
    HostName <gaming-PC-IP>
    User <gaming-PC-Windows-user>
    IdentityFile ~/.ssh/slopstation
```

Set `sshHost` in `config.json` to the alias. Test only an allowed verb:

```powershell
ssh gaming-pc status
```

An arbitrary command should answer `DENIED`.

### Acceptance checks

Run the gaming-PC doctor:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\CouchGaming\Doctor.ps1
```

Then exercise:

1. one enter and exit with the TV already on;
2. one full launch with the gaming PC asleep and TV off;
3. a second installer run, which should make no material change; and
4. a normal deployment, which must preserve `config.psd1`.

Do not merge a change that rewrites tasks or live configuration until these
checks pass on the gaming PC.

## Optional media stack

The media services are independent of the gaming path. Follow
[../media/README.md](../media/README.md) after the mini PC installation is
healthy.
