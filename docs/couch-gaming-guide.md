# Couch Gaming Build Guide — Complete Step-by-Step (v3.0)

From boxes to a one-chord Steam console: RTX 4090 → direct HDMI → Samsung S90C, orchestrated by a GMKtec K15, native 2026 Steam Controller via Puck passthrough, Ex-Link TV control (port confirmed present).

**Starting state:** Gaming PC and TV set up and working normally. Still boxed: K15, 2.5G switch, Cat6, 25 ft optical HDMI, SH-U35B serial cable. Steam Controller + Puck on hand.

**Design decisions locked in** (from v2 + review): every Windows logon unconditionally restores OFFICE mode (no boot-gating flag); SSH is signaling only — all display/Steam work runs via interactive Scheduled Tasks; TV powers on early (EDID needs to be live) but switches input **last**; the Puck/VirtualHere handoff is validated before any automation is written, because it's the one remaining architectural unknown.

Version 4.4 (as-built) · August 9, 2026 · This document matches the deployed system file-for-file: every script is the final field-tested version and every command listed was actually required.
*(v4.4: Exit no longer blanks the desk monitor at teardown — the display's own power-plan timeout handles it. v4.3: session lock gains a heartbeat — a K15 crash or closed console mid-session goes stale and is recycled after 5 minutes instead of silently blocking every future launch. v4.2: Wake-Safety resume task cleans up sessions abandoned without End TV Session and distinguishes keyboard from network wakes; Enter recycles stale Puck claims before claiming; DisplayMagician instances killed after every verified apply (frozen-window prevention); Ctrl+Alt+E desk hotkey for Exit. v4.1 hardened after the first failure drill; v4.0 consolidated the original build.)*

---

## Stage 0 — Conventions (10 min, at any keyboard)

All values below are the real ones for this build — every command and script in this guide already uses them; nothing needs substituting.

| Item | Value |
|---|---|
| Gaming PC hostname | `TILLMAN-DESKTOP` |
| Gaming PC IP (reserved) | `192.168.68.67` |
| Gaming PC Ethernet MAC | `74-56-3C-45-92-DD` |
| Windows account on gaming PC | `tillm` |
| K15 hostname / account | `K15` / `minipc` |
| K15 IP (reserved) | `192.168.68.75` |
| Puck's VirtualHere address | `K15.5` (VID `28DE`, PID `0x1304` via Puck) |
| Ex-Link serial port on K15 | `COM3` |
| TV EDID name in Windows | `QCQ90S` |
| TV inputs | HDMI1 = Apple TV · HDMI2 = PS5 · HDMI3 = eARC · HDMI4 = PC |

Folder layout, both machines: `C:\CouchGaming\` with `logs\` and (K15 only) `state\` subfolders. Keep all scripts, shortcuts, and config there.

---

## Stage 1 — Physical install & network (1–2 h)

1. **Switch:** place the 2.5G switch near the gaming PC/router. Uplink: one Cat6 from a Deco LAN port → switch. Gaming PC Ethernet → switch (this link should negotiate 2.5G if the PC NIC supports it — check later in adapter status).
2. **Long Cat6 run:** switch → TV room, routed around the doorway. This and the HDMI can share a raceway.
3. **Long HDMI run:** the optical cable is **directional** — the end marked *Source* (or *1*) plugs into the RTX 4090's single HDMI port; *Display* (or *2*) into the TV's **HDMI 4**. Respect the minimum bend radius; optical cables die at kinks, not at length.
4. **Ultrawide:** if it's currently on the 4090's HDMI port, move it to DisplayPort now (the 4090 has exactly one HDMI 2.1 out and the TV needs it).
5. **K15 placement:** in the TV room, near the TV. Connect: Cat6 → K15, power. Leave USB free for the Puck and the SH-U35B.
6. **SH-U35B:** USB end → K15, 3.5 mm end → the TV jack marked **EX-LINK**. (You've confirmed it exists. It's control-only — it never touches the video path.)
7. **Puck:** stays boxed until Stage 4. First pairing happens at the gaming PC.
8. **Deco app:** create DHCP reservations for the gaming PC and the K15 at the addresses you chose in Stage 0.

---

## Stage 2 — K15 first boot: make it an appliance (1 h)

The K15 has three jobs — orchestrator, VirtualHere host, media server — and one requirement: it's *always* available.

1. Temporarily plug the K15 into a spare TV input (or any monitor) with a short HDMI for setup. Complete Windows 11 OOBE with a **local account** (simplest for a headless box), hostname `K15`.
2. Windows Update fully; install the FTDI VCP driver if the SH-U35B doesn't enumerate on its own (check Device Manager → Ports).
3. **Power settings:** Settings → System → Power → Never sleep, never turn off (plugged in). In Control Panel power options, disable hibernate too: `powercfg /h off` (elevated).
4. **Auto-logon:** run `netplwiz`, untick "Users must enter a user name and password" (or use Sysinternals Autologon). The orchestrator and listeners run in the user session, so the K15 must land on the desktop unattended after power loss.
5. **Remote management:** Settings → System → Remote Desktop → On. From now on, manage the K15 over RDP; the short HDMI can stay connected to a spare TV input as a maintenance/fallback path or come out.
6. Install Python 3.12+ (python.org installer, "Add to PATH" checked), then: `pip install pyserial hidapi`. (Use `hidapi`, not the similarly-named `hid` package — `hid` is bindings-only and fails at import with a missing-DLL error; `hidapi` bundles the native library.)
7. Verify: from the gaming PC, `ping K15` works; from the K15, `ping 192.168.68.67` works.

---

## Stage 3 — Prove the video path + fix TV behavior (1–2 h, mostly soak time)

No software yet. A flaky cable produces symptoms identical to broken scripting later — eliminate that first.

### 3.1 TV input settings (do these before judging picture)

1. **Settings → Support → About This TV** — record the exact model code (e.g. QN77S90CAFXZA).
2. **Input Signal Plus → ON for HDMI 4** (Settings → All Settings → Connection → External Device Manager → Input Signal Plus). Without this the port won't run full-bandwidth 4K120 RGB.
3. On the Home/Connected Devices screen, **edit HDMI 4's name/icon to "PC"** — this enables proper 4:4:4 chroma handling.
4. **Game Mode: On/Auto** (under Game Mode Settings / External Device Manager — exact menu location varies slightly by firmware).
5. **Disable automatic HDMI source switching.** Update TV firmware first. Then, with the TV on the *TV/live* source (not an HDMI input), press on the Samsung remote: **Mute → Volume Down → Channel Down → Mute**. There's no confirmation UI. Verify afterwards: power the PC on/off a few times during 3.2 and confirm the TV never jumps inputs on its own. Leave **Anynet+ (CEC) on** — the Apple TV uses it, and the PC path doesn't (GPUs have no CEC, which is also why turning the TV on can never affect the PC).

### 3.2 Video soak test

Manually (Windows Display Settings on the gaming PC, TV on HDMI 4):

1. Duplicate/extend to the S90C, then set it to **3840×2160 @ 120 Hz, SDR**. Play games for 30–60 min. Watch for blackouts, flashes, link drops.
2. Then enable **VRR / G-Sync Compatible** (NVIDIA Control Panel → Set up G-SYNC → enable for the TV). Soak again.
3. Then enable **HDR** (Windows HD Color). Soak again.
4. While you're here, note the TV's EDID name as Windows reports it (Display Settings → the display's name, or `Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID` in PowerShell) — you'll need it for a detection check in Stage 8.
5. Optional stretch goal, later, not now: 144 Hz. 120 is the deliberately boring compatibility target for cable + HDR + VRR; revisit 144 only after weeks of stability.

**If the link is flaky:** reseat both ends (verify Source/Display orientation), drop to 60 Hz to distinguish bandwidth vs. link problems, and test the cable on a short run before blaming anything else. Do not proceed to automation with a marginal cable.

When done, set the ultrawide back as the only display for now.

---

## Stage 4 — Controller pairing, then the VirtualHere go/no-go (1–2 h) ⚠️ architectural gate

This is the most important experiment in the project. VirtualHere forwards raw USB, so the gaming PC *should* see the literal Puck and Steam *should* treat the controller as fully native — but nobody documents the 2026 Puck specifically. Prove it before writing a line of automation.

### 4.1 Pair + firmware at the gaming PC (local, no network tricks)

1. Snap the Puck onto the controller magnetically.
2. Plug the Puck into the **gaming PC** with the bundled USB-C cable and open Steam.
3. Follow the prompts to update Puck firmware, then controller firmware (Steam will ask you to move the USB-C between Puck and controller mid-process).
4. Verify everything native and local: trackpads, gyro, grip buttons (L4/L5/R4/R5), haptics, Steam Input config.
5. Leave the controller in **Puck mode** — don't set up Bluetooth (higher latency, and it would add a manual mode-toggle to every session).

### 4.2 VirtualHere install — server on the K15

1. On the **K15**, download the VirtualHere Windows **server** — the file is `vhusbdwinw64.exe` (Intel/AMD 64-bit build) — into `C:\CouchGaming\`.
2. Double-click it once and confirm it runs (tray icon appears; the hub becomes visible from the client in 4.3).
3. Make it survive reboots. Preferred: install it as a service from an **admin** terminal:

   ```
   cd C:\CouchGaming
   .\vhusbdwinw64.exe -b
   ```

   Verify with `Get-Service *virtualhere*` → Status should be `Running`. If `-b` complains, check `.\vhusbdwinw64.exe --help` for the current service flag — or skip the service entirely and drop a shortcut to the exe in the Startup folder (Win+R → `shell:startup`), which is equivalent on this box since Autologon guarantees a session.
4. No settings need changing; as a service it runs headless and its defaults are correct. (Licensing: the free server shares exactly **one** USB device — the Puck is that device, and since one Puck supports up to 4 controllers, even future multiplayer stays free.)

### 4.3 VirtualHere install — client on the gaming PC

1. On the **gaming PC**, download the VirtualHere **client** (`vhui64.exe`) into `C:\CouchGaming\` and run it.
2. The client is a single small window, entirely right-click driven. Right-click the root **USB Servers** line → tick **Start minimized**. If the menu has no start-at-boot option, add a shortcut to `vhui64.exe` in the Startup folder (Win+R → `shell:startup`).
3. Same right-click menu → **Specify USB Server…** → enter `192.168.68.75:7575`. A directly-specified hub reconnects via aggressive direct TCP after the PC wakes from sleep, ~10 s faster than waiting for Auto-Find's broadcast discovery (measured: the Enter script's "VirtualHere sees Puck" gate dropped from ~15 s to ~5 s on the wake path). Leave Auto-Find on as a fallback.
4. You'll see every USB device the K15 is sharing — including ones you must **never claim**:
   - **FT232R USB UART** = the SH-U35B serial cable. It must stay local to the K15 (it's the TV-control COM port). Being listed is harmless; claiming it would break Ex-Link.
   - Any internal radio (may appear as **Wireless_Device** or similar) = the K15's own Bluetooth. Leave it alone.
5. Right-click **USB Servers** → **Advanced Settings** and tune reconnect timing for the wake-from-sleep path (apply by exiting and relaunching the client): **Ping tab** → Server Ping Period `2`, Server Ping Timeout `5` (faster dead-socket detection after resume; don't go tighter - this same ping is the in-game liveness check). **Lookup tab** → Look for new servers every `5` (the default 30 s cycle is the main source of reconnect variance after wake). Leave the Auto-Use tab untouched.
6. **Never enable Auto-Use or Auto-Use All Devices.** Auto-claim would steal the Puck whenever the PC is awake at your desk, deafening the couch trigger — and would grab the serial cable too. Every claim in this system is explicit, made by the scripts.

### 4.4 The go/no-go test

1. Move the Puck from the gaming PC to a **K15 USB-A port**.
2. On the gaming PC, a new device appears in the client under the K15 hub. Confirm it's the Puck: right-click → Properties → vendor ID **28DE** (Valve).
3. Get its address from an ordinary terminal on the gaming PC:

   ```
   C:\CouchGaming\vhui64.exe -t "LIST"
   ```

   The Puck's address is **`K15.5`** — the scripts below already use it. (If it ever changes, e.g. after a firmware update, `LIST` is the source of truth.)
4. Claim it (substitute your real address):

   ```
   C:\CouchGaming\vhui64.exe -t "USE,K15.5"
   ```

   Windows plays the device-connect chime; Steam shows a Steam Controller connecting. (Right-click → **Use** in the client window does the same thing — the command line is shown because the scripts will use it.)
5. In Steam, verify **everything** again, now through the wall: trackpads, gyro, grips, haptics, rumble, Steam Input remapping.
6. Play something twitchy for 20–30 minutes and judge latency honestly. Wired-LAN USB forwarding adds ~1–2 ms; it should feel indistinguishable from Stage 4.1.
7. Release it:

   ```
   C:\CouchGaming\vhui64.exe -t "STOP USING,K15.5"
   ```

   The device should return to the K15 within a few seconds.
8. Repeat the claim/release cycle **five more times**. It must be boring.

### 4.5 Verdict

**GO:** Steam sees a native Steam Controller with full functionality through VirtualHere → the architecture is green-lit; everything after this stage is plumbing.

**NO-GO:** the Puck misbehaves under forwarding (missing inputs, disconnects, laggy feel) → stop and reassess the controller path before building any automation. Fallbacks, in order: Bluetooth pairing direct to the gaming PC through the wall (works, but worse latency), or a USB-over-Cat6 extender for the Puck (new hardware). Don't build the automation on a red gate.

## Stage 5 — Ex-Link TV control from the K15 (30–60 min)

Port confirmed present, cable confirmed correct: Samsung's jack transmits on tip / receives on ring; the SH-U35B is Tip=RXD, Ring=TXD from the adapter side — a straight match, no crossover needed.

1. On the K15, Device Manager → Ports → note the **USB Serial Port (COMx)** number.
2. Serial parameters: **9600 baud, 8N1, no flow control** (community-established for consumer Ex-Link; 115200 on these ports is debug console, not control).
3. Create `C:\CouchGaming\exlink.py`:

```python
import sys, serial

PORT = "COM3"  # FTDI SH-U35B on the K15
CMDS = {
    "power_on":  "082200000002d4",
    "power_off": "082200000001d5",
    "hdmi1": "08220a000500c7",
    "hdmi2": "08220a000501c6",
    "hdmi3": "08220a000502c5",
    "hdmi4": "08220a000503c4",
}
# Frame: 08 22 c1 c2 c3 value + checksum, checksum = (0x100 - sum(first 6)) & 0xFF

def send(name):
    with serial.Serial(PORT, 9600, timeout=1) as s:
        s.write(bytes.fromhex(CMDS[name]))
        resp = s.read(3)
    print(f"{name}: sent, response={resp.hex() or '(none)'}")
    return resp

if __name__ == "__main__":
    send(sys.argv[1])
```

4. Validate, in this order, from a K15 terminal:
   - TV **on**, watching anything: `python exlink.py power_off` → TV turns off. A `030cf1` response = command acknowledged.
   - TV in **standby**: `python exlink.py power_on` → TV turns on. (The port is powered in standby on native-Ex-Link models. If — and only if — power-on from standby fails, check the TV's eco/power-saving settings, and note the network-wake fallback below.)
   - TV on any source: `python exlink.py hdmi4` → discrete jump to HDMI 4, no source-menu cycling. Confirmed mapping on this set: `hdmi1` = Apple TV, `hdmi2` = PS5, `hdmi3` = eARC (soundbar), `hdmi4` = PC. Teardown returns to `hdmi1`.
5. **Do not enter the Samsung service menu.** On native-port models it's plug-and-play; service-menu steps in older guides apply to USB-dongle models. If commands genuinely don't land after step 4: temporarily toggle Anynet+ off and retest (rare interference reports), and double-check you're in the EX-LINK jack, not a look-alike service jack.

**Fallback (keep in your pocket, don't build now):** Tizen local WebSocket control (`samsungtvws`) + SmartThings for discrete input selection, with network wake ("Power On with Mobile"). Only needed if Ex-Link ever proves unreliable.

---

## Stage 6 — Display profiles + the OFFICE fail-safe (1 h)

### 6.1 Profiles (gaming PC)

Install **DisplayMagician** (latest stable, GitHub). Then:

**OFFICE**
1. TV off or on Apple TV; long HDMI stays connected.
2. Windows Display Settings → select the S90C → *Disconnect this display* (genuinely disabled, not secondary).
3. Ultrawide primary, native res/refresh; default audio = desk speakers/headset.
4. Save profile as `OFFICE`; create its permanent-switch shortcut at `C:\CouchGaming\OFFICE.lnk`.

**TV-GAMING**
1. TV on, HDMI 4 (use the remote for setup).
2. Enable the S90C, make it primary at **3840×2160 @ 120 Hz**; disable the ultrawide. HDR/VRR per your Stage 3 results.
3. Default audio = NVIDIA HDMI → S90C.
4. Save as `TV-GAMING`; shortcut at `C:\CouchGaming\TV-GAMING.lnk`.

Do **not** use extend, and don't use DisplayMagician's auto-rollback game shortcuts — the session scripts own the lifecycle.

Test: double-click each shortcut alternately **10 times**. Then reboot twice and sleep/wake twice with the TV both on and off; ordinary use must always come back ultrawide-only. Include one adversarial case: leave the TV powered on and manually sitting on HDMI 4, reboot the PC, and confirm Windows still comes back ultrawide-only.

### 6.2 The fail-safe: OFFICE at every logon, unconditionally

`C:\CouchGaming\Office-Safety.ps1` — verify-and-retry, not fire-and-forget: on every normal boot it confirms office and exits instantly; after a crash that left the TV-primary topology it applies OFFICE with up to 3 verified attempts (DisplayMagician's first post-boot launch is its slowest — a single blind attempt can miss), and logs itself so unattended recoveries leave evidence:

```powershell
$probe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $enc | Select-Object -Last 1) }
Start-Transcript "C:\CouchGaming\logs\office-safety-$(Get-Date -Format yyyyMMdd-HHmmss).log"
for ($try = 1; $try -le 3; $try++) {
    if ((Get-PrimaryHeight) -ne 2160) { Write-Host "office confirmed (attempt $try)"; break }
    Write-Host "TV is primary - applying OFFICE (attempt $try)"
    Start-Process 'C:\CouchGaming\OFFICE.lnk'
    $end = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $end -and (Get-PrimaryHeight) -eq 2160) { Start-Sleep -Milliseconds 500 }
}
if ((Get-PrimaryHeight) -eq 2160) { Write-Host 'WARNING: OFFICE never took after 3 attempts' }
Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item 'C:\ProgramData\CouchGaming\ready' -ErrorAction SilentlyContinue
Stop-Transcript
```

Known gap the task cannot close: the Windows **login screen** renders before any at-logon task and uses the last-used topology — after a hard kill mid-TV-session, the sign-in prompt appears on the TV. Recovery: Win+P works on the login screen (pick "PC screen only"), or sign in facing the TV; OFFICE converges ~20-60 s after logon. Optional closure: auto-logon on the gaming PC (netplwiz), trading physical-access security for never showing a prompt on the TV.

Register it (elevated PowerShell on the gaming PC):

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' `
     -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\CouchGaming\Office-Safety.ps1'
$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$t.Delay = 'PT20S'
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'ForceOfficeAtLogon' `
     -Action $a -Trigger $t -RunLevel Highest
```

The invariant this buys: normal boot, update reboot, crash-and-restart, power loss mid-game (where Windows would otherwise come back with the TV-primary topology as last-used) — **all** converge to ultrawide-only at next logon, and the task sends zero TV commands, so a TV that's off stays off and an Apple TV night stays undisturbed. Couch launches aren't affected because the Enter task (Stage 8) can only run *after* logon anyway.

### 6.3 The wake fail-safe: cleaning up abandoned sessions

Resume-from-sleep is not a logon, so `ForceOfficeAtLogon` never fires on it — and a session abandoned without running End TV Session (quit game, walk away, PC idle-sleeps still in TV mode) would otherwise wake into a dark desk with a stale Puck claim (VirtualHere re-acquires the device on reconnect, creating a fresh instance while Steam holds handles to the old one — controller haptics work, inputs dead). `C:\CouchGaming\Wake-Safety.ps1` closes the gap:

```powershell
Start-Transcript "C:\CouchGaming\logs\wake-safety-$(Get-Date -Format yyyyMMdd-HHmmss).log"
Start-Sleep 3
$wake = (powercfg /lastwake | Out-String)
Write-Host $wake
if ($wake -match 'Magic Packet|Ethernet|GbE') {
    Write-Host 'network wake - couch launch owns this; standing down'
} elseif ((Test-Path 'C:\ProgramData\CouchGaming\ready')) {
    Write-Host 'stale TV session detected - running Exit cleanup'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\CouchGaming\Exit-TV.ps1'
} else {
    Write-Host 'clean wake - nothing to do'
}
Stop-Transcript
```

Register on the resume event (elevated PowerShell):

```powershell
schtasks /Create /TN "\CouchGaming\WakeSafety" /SC ONEVENT /EC System /MO "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\CouchGaming\Wake-Safety.ps1"
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'WakeSafety' -Settings $s
```

Semantics: keyboard wake into a stale session → full Exit cleanup (desk restored, Puck released fresh — which also cures the stale-claim controller); normal desk wake → no-op; chord/WoL wake → stands down so Enter owns it. The network-wake match keys on `powercfg /lastwake` text — verify your NIC's wake string matches the regex after the first WoL wake (the transcript prints it raw) and widen the pattern if needed. (The K15 needs no changes for abandoned sessions: its watch loop already declares the session dead ~20 s after the PC sleeps, powers the TV off, and re-arms the chord.)

### 6.4 One sign-in setting

Settings → Accounts → Sign-in options → "If you've been away, when should Windows require you to sign in again?" → **Never**. Resume-from-sleep must land on the desktop, not the lock screen, or the couch flow wakes the PC into a locked session and Big Picture ends up behind it. (Home-LAN tradeoff; if the machine must lock, the alternative is treating "locked" as a launch-abort condition, which degrades the console UX.) Cold-boot auto-logon via `netplwiz` is a further optional step — skip it initially; **Sleep is the couch-ready state.**

---

## Stage 7 — Wake-on-LAN (30 min)

1. BIOS/UEFI: enable *Wake on LAN / Power On by PCI-E*. While there, disable ErP/deep-sleep states that cut standby power to the NIC.
2. Windows Device Manager → Ethernet adapter → Power Management: *Allow this device to wake the computer* + *Only allow a magic packet*. Advanced tab: *Wake on Magic Packet: Enabled*.
3. Confirm `powercfg /a` shows Standby (S3) or Modern Standby available; put the PC to **sleep** (not shutdown).
4. From the K15 (PowerShell one-off or the Python below), send a magic packet and confirm the PC wakes. Repeat 5×, including once after the PC has slept overnight. Do not proceed until it's boring.

---

## Stage 8 — SSH signaling + interactive session tasks (1–2 h)

SSH is the secure RPC channel; the actual display/Steam/USB work runs inside your logged-in desktop session via Scheduled Tasks — GUI and display-topology operations are unreliable from SSH's non-interactive session, so the SSH account is allowed to do exactly one thing: kick tasks.

### 8.1 OpenSSH Server on the gaming PC (elevated PowerShell)

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-NetFirewallRule -Name sshd-k15 -DisplayName 'OpenSSH from K15 only' -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress 192.168.68.75 -Action Allow
Get-NetFirewallRule -Name *OpenSSH* | Disable-NetFirewallRule   # drop the default any-source rule
```

### 8.2 Key + forced command

On the **K15**: `ssh-keygen -t ed25519` (no passphrase, it's an automation key), then copy the contents of `%USERPROFILE%\.ssh\id_ed25519.pub`.

On the **gaming PC** — your account is an Administrator, so Windows sshd reads keys from `C:\ProgramData\ssh\administrators_authorized_keys` (this path trips everyone up). Create that file containing exactly one line:

```
command="powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Dispatch.ps1",no-port-forwarding,no-agent-forwarding,no-x11-forwarding ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIthhg2so2NtfSolLJeG3c4FKaazA2ffokCPPPvcYzOY minipc@K15
```

Then fix its ACL or sshd will ignore it:

```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r `
  /grant "SYSTEM:F" /grant "BUILTIN\Administrators:F"
```

`C:\CouchGaming\Dispatch.ps1` — the entire remote attack surface:

```powershell
switch ($env:SSH_ORIGINAL_COMMAND) {
  'enter'  { schtasks /Run /TN '\CouchGaming\Enter' | Out-Null; 'OK' }
  'exit'   { schtasks /Run /TN '\CouchGaming\Exit'  | Out-Null; 'OK' }
  'status' { if (Test-Path 'C:\ProgramData\CouchGaming\ready')
             { Get-Content 'C:\ProgramData\CouchGaming\ready' } else { 'NOTREADY' } }
  default  { 'DENIED'; exit 1 }
}
```

On the K15, `%USERPROFILE%\.ssh\config`:

```
Host gamepc
  HostName 192.168.68.67
  User tillm
  IdentityFile ~/.ssh/id_ed25519
  ConnectTimeout 5
```

Test from the K15: `ssh gamepc enter` should print `OK` (the task will no-op-fail until 8.4 — fine), `ssh gamepc status` prints `NOTREADY`, `ssh gamepc whatever` prints `DENIED`, and `ssh gamepc` with a shell attempt gets the dispatcher, not a prompt.

### 8.3 Host session scripts

These are the deployed, field-tested versions: condition-polled waits with elapsed-time stamps, display state read via a fresh-process `GetSystemMetrics` probe (WMI display classes and in-process metrics both report stale values inside windowless scheduled tasks), VirtualHere results redirected to a file with `-r` (console-less `-t` calls otherwise throw GUI popups), claims/releases verified by Windows device enumeration with retries (the IPC report can read `FAILED: API Timeout` on an operation that succeeded - enumeration is the arbiter), an immediate client nudge plus a hub-reconnect gate for the wake-from-sleep path (the client's TCP link dies during S3 sleep; claiming before reconnect returns `ERROR: Invalid address`), the Puck claim parallelized with profile settling (profile verification happens after the USB phase), Big Picture forced to the foreground on enter and closed (while still on the TV) as exit's first act. Measured: warm enters ~6-8 s, wake-from-sleep enters ~8-13 s to READY, dominated by S3 resume + VirtualHere reconnect.

`C:\CouchGaming\Enter-TV.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
Start-Transcript "C:\CouchGaming\logs\enter-$(Get-Date -Format yyyyMMdd-HHmmss).log"
$sw   = [Diagnostics.Stopwatch]::StartNew()
$vh   = 'C:\CouchGaming\vhui64.exe'
$puck = 'K15.5'
$vhr  = 'C:\CouchGaming\logs\vh-last.txt'
$probe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $enc | Select-Object -Last 1) }
function Log($m) { Write-Host ("[+{0,5:n1}s] {1}" -f $sw.Elapsed.TotalSeconds, $m) }
function Get-TvNames {
    Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue |
    ForEach-Object { -join [char[]]($_.UserFriendlyName | Where-Object { $_ -ne 0 }) }
}
function Test-PuckPresent {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match 'VID_28DE&PID_1304' -and $_.Status -eq 'OK' })
}
function Get-VhList {
    & $vh -t "LIST" -r $vhr | Out-Null
    Start-Sleep -Milliseconds 400
    (Get-Content $vhr -ErrorAction SilentlyContinue) -join ' '
}
function Wait-For([scriptblock]$Cond, [double]$TimeoutSec, [string]$What) {
    $end = $sw.Elapsed.TotalSeconds + $TimeoutSec
    while ($sw.Elapsed.TotalSeconds -lt $end) {
        if (& $Cond) { Log $What; return $true }
        Start-Sleep -Milliseconds 250
    }
    Log "TIMEOUT waiting for: $What"; return $false
}
try {
    # Kick the VirtualHere client immediately so dead-socket detection + reconnect
    # start now and overlap everything below
    Start-Process -WindowStyle Hidden $vh -ArgumentList '-t','LIST','-r','C:\CouchGaming\logs\vh-nudge.txt'
    Log ("primary height at start: {0}" -f (Get-PrimaryHeight))

    # 1. TV EDID visible (in the real flow the K15 just powered it on)
    if (-not (Wait-For { (Get-TvNames) -match 'QCQ90S' } 30 'TV detected')) {
        throw 'S90C never appeared over HDMI - aborting, office display untouched'
    }

    # 2. Launch the TV-only profile and DON'T wait for it - it settles while we do USB work
    Start-Process 'C:\CouchGaming\TV-GAMING.lnk'
    Log 'TV-GAMING profile launched'

    # 3a. Wait for the VirtualHere client to (re)connect to the K15 hub
    if (-not (Wait-For { (Get-VhList) -match [regex]::Escape($puck) } 30 'VirtualHere sees Puck')) {
        throw 'VirtualHere client never re-connected to the K15 hub'
    }

    # 3b. Claim the Puck - up to 2 attempts, verified by Windows enumeration, not the IPC report
    if (Test-PuckPresent) {
        Log 'stale Puck claim detected - releasing for a fresh instance'
        & $vh -t "STOP USING,$puck" -r $vhr
        Wait-For { -not (Test-PuckPresent) } 6 'stale claim released' | Out-Null
    }
    $claimed = $false
    for ($i = 1; -not $claimed -and $i -le 2; $i++) {
        & $vh -t "USE,$puck" -r $vhr
        $claimed = Wait-For { Test-PuckPresent } 8 "Puck enumerated (attempt $i)"
        Log ("vh attempt {0}: {1}" -f $i, ((Get-Content $vhr -ErrorAction SilentlyContinue) -join ' '))
    }
    if (-not $claimed) { throw 'VirtualHere claim did not produce a device after 2 attempts' }

    # 4. NOW verify the profile actually took (it had the whole USB phase to settle)
    if (-not (Wait-For { (Get-PrimaryHeight) -eq 2160 } 20 'TV is primary (2160p)')) {
        throw 'TV-GAMING profile did not take'
    }
    Start-Sleep -Milliseconds 500   # audio-device settle margin
    Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force   # no lingering instance = no frozen window

    # 5. Big Picture, forced to the foreground
    Start-Process 'steam://open/bigpicture'
    if (-not (Wait-For { Get-Process steam -ErrorAction SilentlyContinue } 20 'Steam running')) {
        throw 'Steam failed to start'
    }
    Start-Sleep 1
    $wsh = New-Object -ComObject WScript.Shell
    $focused = $false
    for ($i = 0; -not $focused -and $i -lt 5; $i++) {
        foreach ($t in 'Steam Big Picture Mode','Steam') {
            if ($wsh.AppActivate($t)) { $focused = $true; Log "focused '$t'"; break }
        }
        if (-not $focused) { Start-Sleep 1 }
    }

    # 6. Ready marker - the K15 switches the TV input only after seeing this
    New-Item -ItemType Directory -Force 'C:\ProgramData\CouchGaming' | Out-Null
    Set-Content 'C:\ProgramData\CouchGaming\ready' (Get-Date).ToString('o')
    Log 'READY'
}
catch {
    & $vh -t "STOP USING,$puck" -r $vhr 2>$null
    Start-Process 'C:\CouchGaming\OFFICE.lnk'
    Remove-Item 'C:\ProgramData\CouchGaming\ready' -ErrorAction SilentlyContinue
    throw
}
finally { Stop-Transcript }
```

`C:\CouchGaming\Exit-TV.ps1`:

```powershell
Start-Transcript "C:\CouchGaming\logs\exit-$(Get-Date -Format yyyyMMdd-HHmmss).log"
$sw  = [Diagnostics.Stopwatch]::StartNew()
$vhr = 'C:\CouchGaming\logs\vh-last.txt'
$probe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $enc | Select-Object -Last 1) }
function Log($m) { Write-Host ("[+{0,5:n1}s] {1}" -f $sw.Elapsed.TotalSeconds, $m) }
function Test-PuckClaimed {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match 'VID_28DE&PID_1304' -and $_.Status -eq 'OK' })
}
# Leave Big Picture FIRST, while still on the TV - Steam's window never gets
# resolution-yanked mid-render (prevents a garbled desktop-Steam window)
Start-Process 'steam://close/bigpicture'
Log 'closing Big Picture'
Start-Sleep 2
Start-Process 'C:\CouchGaming\OFFICE.lnk'   # office first: controller stays live during teardown
Log 'OFFICE profile launched'
while ($sw.Elapsed.TotalSeconds -lt 15 -and (Get-PrimaryHeight) -eq 2160) {
    Start-Sleep -Milliseconds 250
}
if ((Get-PrimaryHeight) -eq 2160) {
    Log 'office did not take - retrying'
    Start-Process 'C:\CouchGaming\OFFICE.lnk'; Start-Sleep 5
} else { Log 'ultrawide restored' }
Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force   # no lingering instance = no frozen window

# Release the Puck; retry until Windows agrees it's gone
$released = $false
for ($i = 1; -not $released -and $i -le 3; $i++) {
    & 'C:\CouchGaming\vhui64.exe' -t 'STOP USING,K15.5' -r $vhr
    Start-Sleep 1
    Log ("vh attempt {0}: {1}" -f $i, ((Get-Content $vhr -ErrorAction SilentlyContinue) -join ' '))
    $released = -not (Test-PuckClaimed)
    if (-not $released) { Start-Sleep 2 }
}
if ($released) { Log 'Puck released' } else { Log 'WARNING: Puck may still be claimed - check VirtualHere client' }

# Repaint guard: minimize desktop Steam so it re-lays-out fresh (at the ultrawide's
# resolution) the next time it's opened - prevents the stale-4K garbled window
Add-Type -Namespace P2 -Name W -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
$sp = Get-Process steam -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($sp) { [void][P2.W]::ShowWindow($sp.MainWindowHandle, 6); Log 'Steam minimized' }   # 6 = SW_MINIMIZE

Remove-Item 'C:\ProgramData\CouchGaming\ready' -ErrorAction SilentlyContinue
Log 'done'
Stop-Transcript
```

Reading the transcripts (`C:\CouchGaming\logs\`): a healthy enter ends `READY`, a healthy exit ends `Puck released`. A line like `vh attempt 1: FAILED: API Timeout 3 sec` immediately after a successful `Puck enumerated` gate is documented cosmetic noise - the first IPC call after a display switch often times out on the report even when the operation succeeded; the enumeration checks are the source of truth.

### 8.4 Register the tasks (interactive session, on demand)

```powershell
foreach ($n in 'Enter','Exit') {
  $a = New-ScheduledTaskAction -Execute 'powershell.exe' `
       -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\CouchGaming\$n-TV.ps1"
  $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
  Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName $n -Action $a -Settings $s
}
```

The 5-minute execution limit is load-bearing: Task Scheduler ignores start requests for a task that is "currently running," so any hung instance would otherwise silently kill every future run — the tile keeps reporting success while nothing happens. The limit makes a wedge self-clear. (`-RunLevel Highest` is deliberately absent: neither script needs elevation, and an elevated task can't be started by non-elevated Steam — the End TV Session tile would fail with Access denied.)

```powershell
# already registered with -RunLevel Highest and/or no time limit? Fix in place:
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Exit'  -Settings $s
Set-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Enter' -Settings $s
```

Registering as the current user with no stored credentials makes them **run only when you're logged on, in your interactive session** — exactly what display switching and Steam need. `schtasks /Run` on them fails if no session exists yet, which the K15 treats as "not ready, retry" — that's the mechanism that serializes couch launches *after* logon and after `ForceOfficeAtLogon`.

Test locally at the PC: run the Enter task from Task Scheduler with the TV on → TV-only + Puck claimed + Big Picture. Run Exit → office restored, Puck released. Ten times. Check the transcripts in `logs\` for anything ugly.

---

## Stage 9 — K15 orchestrator (1–2 h)

`C:\CouchGaming\config.json` on the K15:

```json
{
  "gamingPcMac": "74-56-3C-45-92-DD",
  "gamingPcIp":  "192.168.68.67",
  "sshHost":     "gamepc",
  "tvComPort":   "COM3",
  "tvGamingCmd": "hdmi4",
  "tvIdleCmd":   "hdmi1",
  "tvOffWhenDone": true
}
```

(`tvOffWhenDone: true` = TV powers off after sessions; set `false` to return to the Apple TV on `tvIdleCmd: hdmi1` instead.)

`couch.py` (lives next to `config.json` — on this build, the K15 desktop). Every line it prints is also appended to `couch.log` beside it, so chord-launched runs (whose console closes with them) always leave a trail; each `ssh` poll tolerates transient failures and retries rather than aborting the launch; and the session lock carries a heartbeat (touched on every poll) so a lock whose owner died is recycled after 5 minutes rather than blocking launches forever:

```python
import json, pathlib, socket, subprocess, sys, time
import serial

BASE = pathlib.Path(__file__).parent
CFG  = json.loads((BASE / "config.json").read_text())
LOCK = BASE / "state" / "session.lock"
LOCK_STALE_S = 300   # a live session touches the lock every few seconds; much older = dead owner
LOGF = BASE / "couch.log"

EXLINK = {"power_on": "082200000002d4", "power_off": "082200000001d5",
          "hdmi1": "08220a000500c7", "hdmi2": "08220a000501c6",
          "hdmi3": "08220a000502c5", "hdmi4": "08220a000503c4"}

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOGF.open("a", encoding="utf-8") as f: f.write(line + "\n")
    except OSError: pass

def touch_lock():
    try: LOCK.write_text(str(time.time()))
    except OSError: pass

def exlink(name):
    try:
        with serial.Serial(CFG["tvComPort"], 9600, timeout=1) as s:
            s.write(bytes.fromhex(EXLINK[name]))
            log(f"exlink {name} -> {s.read(3).hex() or 'no-ack'}")
    except Exception as e:
        log(f"exlink {name} FAILED: {e}")   # non-fatal: PC readiness is independent

def wol():
    mac = bytes.fromhex(CFG["gamingPcMac"].replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
    log("WOL sent")

def ssh(cmd, timeout=15):
    r = subprocess.run(["ssh", CFG["sshHost"], cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()

def wait_port(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((CFG["gamingPcIp"], 22), 3):
                return True
        except OSError:
            time.sleep(1)
    return False

def start():
    try:
        age = time.time() - LOCK.stat().st_mtime
    except OSError:
        age = None                      # no lock (or it vanished mid-check)
    if age is not None and age < LOCK_STALE_S:
        log("session already active/starting - ignoring"); return 1
    if age is not None:
        log(f"stale session lock ({age:.0f}s old, owner dead) - recycling")
    LOCK.parent.mkdir(exist_ok=True); touch_lock()
    try:
        log("=== LAUNCH ===")
        exlink("power_on")
        wol()
        if not wait_port(): raise RuntimeError("gaming PC never became reachable")
        log("ssh port up")
        for _ in range(60):
            touch_lock()
            try:
                if ssh("enter") == "OK":
                    log("enter dispatched"); break
            except Exception as e:
                log(f"enter attempt failed ({e}) - retrying")
            time.sleep(1)
        else: raise RuntimeError("could not trigger Enter task")
        end = time.time() + 120
        ready = False
        while time.time() < end:
            touch_lock()
            try:
                st = ssh("status")
                if st != "NOTREADY":
                    log(f"host READY ({st})"); ready = True; break
            except Exception as e:
                log(f"status poll failed ({e}) - retrying")
            time.sleep(1)
        if not ready: raise RuntimeError("host never reported READY")
        exlink(CFG["tvGamingCmd"])
        log("=== GAMING ==="); watch()
    except Exception as e:
        log(f"launch failed: {e} - TV input untouched")
        LOCK.unlink(missing_ok=True); return 1
    return 0

def watch():
    fails = 0
    while True:
        time.sleep(5)
        touch_lock()
        try:
            st = ssh("status"); fails = 0
            if st == "NOTREADY":
                log("host reports session ended"); break
        except Exception:
            fails += 1
            if fails >= 3:
                log("gaming PC gone (slept/crashed) - treating as ended"); break
    exlink("power_off" if CFG["tvOffWhenDone"] else CFG["tvIdleCmd"])
    LOCK.unlink(missing_ok=True)
    log("=== IDLE ===")

if __name__ == "__main__":
    sys.exit(start() if (len(sys.argv) < 2 or sys.argv[1] == "start") else 0)
```

Sequencing recap, because it *is* the product: TV powers on **early** (so Windows can see its EDID, and the viewer keeps watching whatever was on); WoL in parallel; `enter` retries until your Windows session exists (which guarantees `ForceOfficeAtLogon` already ran on a cold boot); the input switch to HDMI 4 happens **only after** the host writes READY — so the one visible transition lands on a working Big Picture. Any failure before READY leaves the TV exactly as it was.

The watch loop polls every 5 s, so the TV returns to Apple TV within seconds of session end. The watch loop is also the end-of-session detector: the Exit task removes the `ready` marker (or the PC sleeps and SSH dies), the K15 notices within ~a minute, restores the TV to Apple TV (or off), and unlocks for the next launch.

---

## Stage 10 — Visible launcher + failure drills (1 evening)

1. **K15 launcher** — Notepad on the K15:

   ```
   cd /d "%~dp0"
   python couch.py start
   pause
   ```

   Save As `"C:\Users\minipc\Desktop\Start-TV-Gaming.bat"` (All Files), next to `couch.py` (`%~dp0` = "this .bat's folder"; `pause` keeps the window open so the log is readable). This stays on the desktop forever as the recovery trigger even after the chord exists.

   **"End TV Session" tile in Big Picture** — do NOT add the .bat to Steam directly; bare `.bat`/`.lnk` adds silently fail to appear in the library. The reliable method is adding a real exe with launch options:
   - Steam (desktop) → **Add a Game → Add a Non-Steam Game → Browse** → paste `C:\Windows\System32\schtasks.exe` in the File name box → Open → confirm **schtasks** is checked → **Add Selected Programs**.
   - Library → right-click it → **Properties** → Name: `End TV Session`, **Launch options:** `/Run /TN \CouchGaming\Exit` (no quotes — the path has no spaces, and unquoted leaves nowhere for smart-quote corruption to hide).
   - Test from desktop Steam once (brief console blink + fresh `exit-*.log`), then in Big Picture highlight the tile → ☰ menu button → **Add to Favorites**. It also self-pins via Recent Games since it runs every session.
   - **Desk hotkey for the same teardown:** gaming-PC desktop → New → Shortcut → location `C:\Windows\System32\schtasks.exe /Run /TN \CouchGaming\Exit` → name "Exit TV Mode" → Properties → Shortcut key **Ctrl+Alt+E**, Run: Minimized. Works blind from the desk keyboard regardless of which display is live (hotkey shortcuts must live on the Desktop or Start Menu to register).
   - Designed exit is quit-game-then-tile; pressing the tile with a game still running gets Steam's close-the-running-game prompt (or a no-op on some client builds), per Steam's one-title-at-a-time rule.
   - **Troubleshooting a silent tile** (presses "succeed", nothing happens, no new exit log): run `schtasks /Run /TN \CouchGaming\Exit` in a terminal — the line `INFO: scheduled task ... is currently running` means a wedged task instance is eating every start request. `schtasks /End /TN \CouchGaming\Exit` unsticks it; the 5-minute execution limit (8.4) makes future wedges self-clear.

2. Run the happy path from all four starting states — the full matrix:
   - PC awake + TV on (Apple TV)
   - PC asleep + TV on (Apple TV)
   - PC awake + TV off
   - PC asleep + TV off ← the real acceptance scenario
   (When the PC is already awake at the desk, the couch trigger deliberately wins — it's explicit.)
3. **Failure drills** — each must leave the TV on its prior source and the office recoverable:
   - Pull the gaming PC's Ethernet, trigger a launch → K15 times out, TV never switches.
   - Rename the Enter task temporarily → `enter` never returns OK → clean abort.
   - Unplug the Puck from the K15, launch → Enter's claim fails → office restored, TV untouched.
   - Kill Steam mid-launch → Enter's catch block restores OFFICE.
   - Hard-reset the PC mid-game → next logon lands in OFFICE; K15 watch loop restores the TV.
   - Mash the launcher three times fast → one session, two "already active" logs.
4. Only after all drills pass, append a hard-sleep (`Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)`) to the end of `Exit-TV.ps1` if you want the PC to doze immediately after sessions.

Live with the visible launcher for **a week** before Stage 11.

---

## Stage 11 — The controller chord

The trigger: **hold Steam + right-trigger (full pull) for 2 s** while idle. Valve doesn't document the controller's HID report format, so this stage is calibrate-then-wire: discover the button byte empirically, then run a listener that watches for it. The Puck is local to the K15 whenever a session isn't running — that's when the chord is audible.

### 11.1 Calibrate the report bytes

`calibrate.py` (next to `couch.py`): learns which bytes are sensor noise during a 3 s hands-off window, filters to the dominant report type, then prints only meaningful changes.

```python
import time
import hid

VID, PID = 0x28DE, 0x1304   # Valve; Puck PID from VirtualHere device properties

def pick_interface():
    for d in hid.enumerate(VID, PID):
        try:
            h = hid.device(); h.open_path(d["path"]); h.set_nonblocking(True)
            t0 = time.time(); got = 0
            while time.time() - t0 < 2.0:
                if h.read(64): got += 1
                time.sleep(0.005)
            if got > 10:
                print(f"using interface: {d['path']}")
                return h
            h.close()
        except (OSError, ValueError):
            pass
    return None

dev = pick_interface()
if not dev:
    print("no streaming interface found"); raise SystemExit(1)

print("learning noise for 3s - hands OFF, controller flat...")
reports = []
t0 = time.time()
while time.time() - t0 < 3.0:
    r = dev.read(64)
    if r: reports.append(bytes(r))
    time.sleep(0.002)

from collections import Counter
rid = Counter(r[0] for r in reports).most_common(1)[0][0]
reports = [r for r in reports if r[0] == rid]
n = min(len(r) for r in reports)
base = reports[-1]
noisy = {i for i in range(n) for r in reports if r[i] != base[i]}
stable = [i for i in range(n) if i not in noisy]
print(f"report type: {rid:02x}   ignoring noisy bytes: {sorted(noisy)}")
print("press ONE input at a time. Ctrl+C to quit.\n")

last = base
while True:
    r = dev.read(64)
    if r:
        r = bytes(r)
        if len(r) >= n and r[0] == rid and r != last:
            diffs = [f"byte[{i}]: {base[i]:02x}->{r[i]:02x}" for i in stable if r[i] != base[i]]
            if diffs:
                print("  ".join(diffs))
            last = r
    time.sleep(0.002)
```

Run it (controller awake and flat), press Steam alone, then the right trigger alone, and record the (byte index, bit) each toggles. **Measured on this controller:** both digital bits live in `byte[4]` — Steam = `0x01`, right-trigger full-pull click = `0x80` (bytes 8/9 are the trigger's analog travel; ignore them). If a firmware update ever shuffles the layout, the chord goes quiet and re-running this calibration is the five-minute fix.

### 11.2 The listener

`chord_listener.py` (next to `couch.py`). It holds the Puck's HID interfaces open through controller sleep so a chord from a cold controller is heard the moment the controller reconnects (~1 s); individual unreadable interfaces are culled quietly (the Puck exposes ~13 and some error on read); and it latches onto the input interface **by content** — only reports of the hardcoded input type `RID_INPUT = 0x42` count. Never auto-learn the report type at startup: multiple interfaces stream different report types and whichever answers first wins a race (a status stream, type 0x79, was observed winning — chord deaf until restart). `0x42` comes from calibrate.py's "report type" line; re-derive it there if firmware ever changes it. "Device vanished" fires only when the pool empties or the latched input interface dies — which is exactly the VirtualHere claim, i.e. the session boundary. `couch.py`'s session lock guards against double-fires.

```python
import subprocess, time
import hid

VID, PID = 0x28DE, 0x1304
RID_INPUT = 0x42                  # input report type, from calibrate.py ("report type: 42")
BTN_BYTE = 4
CHORD    = 0x01 | 0x80            # Steam + right-trigger click
HOLD_S   = 2.0
COUCH    = r"C:\Users\minipc\Desktop\couch.py"

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

class Puck:
    def __init__(self): self.handles, self.active = [], None
    def open_all(self):
        self.close()
        for d in hid.enumerate(VID, PID):
            try:
                h = hid.device(); h.open_path(d["path"]); h.set_nonblocking(True)
                self.handles.append(h)
            except (OSError, ValueError): pass
        return bool(self.handles)
    def close(self):
        for h in self.handles:
            try: h.close()
            except Exception: pass
        self.handles, self.active = [], None
    def read_input(self):
        """Return the next report of type RID_INPUT, or None. Latch onto the
        interface that produces them; ignore all other report streams."""
        if self.active is not None:
            while True:
                r = self.active.read(64)         # raises if claimed/unplugged
                if not r: return None
                if r[0] == RID_INPUT: return r   # drain non-input types silently
        for h in list(self.handles):
            try:
                r = h.read(64)
            except (OSError, ValueError):
                self.handles.remove(h)
                try: h.close()
                except Exception: pass
                continue
            if r and r[0] == RID_INPUT:
                self.active = h                  # THE input interface, by content
                return r
        if not self.handles:
            raise OSError("all interfaces gone")
        return None

puck, held, armed = Puck(), None, False
while True:
    if not puck.handles:
        if not puck.open_all():
            time.sleep(1)                        # Puck truly absent (session active)
            continue
        log(f"Puck present - {len(puck.handles)} interfaces open, waiting for controller")
        held, armed = None, False
    try:
        r = puck.read_input()
    except (OSError, ValueError):
        log("device vanished (claimed) - standing by")
        puck.close(); time.sleep(3); continue
    if r:
        if not armed:
            log(f"input stream found (type {RID_INPUT:02x}) - armed")
            armed = True
        if len(r) > BTN_BYTE and (r[BTN_BYTE] & CHORD) == CHORD:
            held = held or time.time()
            if time.time() - held >= HOLD_S:
                log("chord! launching session")
                puck.close()
                subprocess.Popen(["python", COUCH, "start"],
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
                time.sleep(20); held, armed = None, False
        else:
            held = None
    time.sleep(0.005)
```

Dry-run first (swap the `subprocess.Popen` call for a `log(">>> WOULD LAUNCH <<<")`): full chord 2 s fires; single button or a short tap doesn't; a chord held in one motion on a *sleeping* controller still fires (the first press wakes it — hold through the ~1 s reconnect). To force the controller asleep for that test: hold the Steam button ~5 s. Then restore the real launch line and run the lifecycle test: chord-started session → End TV Session tile → listener re-arms on Puck return → second chorded session, hands never touching the K15.

### 11.3 Install at startup

Notepad:

```
cd /d "%~dp0"
python chord_listener.py
pause
```

Save As `"C:\Users\minipc\Desktop\Start-Listener.bat"` — it must live **next to** `chord_listener.py` (`%~dp0` resolves to the .bat's own folder). Then Win+R → `shell:startup` → **right-drag the .bat in → "Create shortcuts here"** — a shortcut (arrow-overlay icon), never the file itself; moving the .bat breaks the relative paths. The `pause` means any future crash leaves its traceback on screen instead of a vanished window.

Verify the unattended chain: reboot the K15 (or pull its power), touch nothing, RDP in — the listener console is on the desktop; tap Steam on the controller → `armed` prints → chord starts a session. One instance only: if you ever launch it manually, make sure the startup copy isn't already running (Task Manager → python.exe). Keep the console minimized, not closed, and keep the standing RDP rule: disconnect, never sign out.

---

## Stage 12 — Media server (independent; any time after Stage 2)

SMB → Infuse, no Jellyfin. All on the K15 over RDP except steps 4–5.

1. **Folders + naming.** Create `C:\Media\Movies` and `C:\Media\TV`. Naming drives Infuse's metadata matching: movies as `Title (Year).mkv` (one file or one subfolder per movie); TV as `Show\Season 01\Show S01E01.mkv`. (The K15 has two spare M.2 slots — when the library outgrows C:, a dedicated SSD changes only the paths.)
2. **Dedicated read-only user** (admin PowerShell):

   ```powershell
   net user media "PickAPassword1" /add /passwordchg:no
   wmic useraccount where "name='media'" set PasswordExpires=false
   ```

3. **Permissions + share** (read-only for `media` — the Apple TV can't delete anything; full for `minipc` — how you drop files):

   ```powershell
   icacls "C:\Media" /grant "media:(OI)(CI)RX"
   New-SmbShare -Name Media -Path C:\Media -ReadAccess "K15\media" -FullAccess "K15\minipc"
   ```

   Confirm the K15's network profile is **Private** (Settings → Network → Ethernet) and file/printer sharing is on for Private profiles (Network and Sharing Center → Advanced sharing settings) — sharing is blocked on Public.
4. **Infuse on the Apple TV:** Settings → Add Files → Other → **SMB** → Address `192.168.68.75`, user `media` + password → pick the `Media` share → Favorite it → let it scan. Playback is direct-play; the K15 only serves bytes.
5. **Dropping files from the gaming PC:** Explorer → `\\192.168.68.75\Media` → credentials `minipc` (tick Remember); optionally map it as a drive letter. Copies run at 2.5 Gb.
6. **Acceptance:** gaming PC asleep → Infuse plays. Media must be fully independent of the gaming stack.

Add Jellyfin later only if a concrete want appears (cross-device watch state, user profiles, phone/web playback, remote access): install it on the K15, point libraries at these same folders, connect Infuse in Direct Mode; the 125U's QuickSync covers any transcoding. Jellyfin is never a prerequisite for anything gaming-related.

---

## Acceptance checklist

**Isolation**
- [ ] Desk boot/reboot/update-reboot: TV never powers on, never changes input, S90C stays disabled in Windows.
- [ ] Desk wake-from-sleep: same.
- [ ] Turning the TV on/off, or manually flipping it to HDMI 4: gaming PC does nothing.

**Session**
- [ ] From PC asleep + TV off: one trigger → Big Picture, controller live, without entering the office.
- [ ] TV stays on its prior source until Big Picture is ready; exactly one visible input transition.
- [ ] Steam reports a native Steam Controller through VirtualHere; trackpads/gyro/grips/haptics intact.
- [ ] "End TV Session" restores the ultrawide, releases the Puck to the K15, returns the TV to Apple TV (or off), then the PC may sleep.

**Failure**
- [ ] Failed wake / failed profile / failed Puck claim / failed Steam: TV input never stolen, office recoverable.
- [ ] Hard crash mid-game: next logon is OFFICE; K15 restores the TV within a minute.
- [ ] Trigger spam: one session.

**Media**
- [ ] Infuse plays from the K15 with the gaming PC asleep.

---

## Quick reference

| Thing | Where |
|---|---|
| Ex-Link frames | `08 22 c1 c2 c3 val + (0x100 − Σ)&0xFF`, 9600 8N1 · on `D4` / off `D5` / HDMI4 `08220A000503C4` |
| VirtualHere claim/release | `vhui64.exe -t "USE,K15.5"` / `"STOP USING,K15.5"` |
| Remote surface | `ssh gamepc enter|exit|status` — nothing else exists |
| Fail-safe | `\CouchGaming\ForceOfficeAtLogon` — unconditional, sends no TV commands |
| The one rule | Nothing switches the TV to HDMI 4 before the host writes READY |
