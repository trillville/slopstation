# Build guide — from boxes to a one-chord console

Everything needed to stand this system up from scratch on identical hardware:
RTX 4090 → direct HDMI → Samsung S90C, orchestrated by a GMKtec K15, native
2026 Steam Controller via Puck passthrough, Ex-Link TV control.

**The scripts in [`gaming-pc/`](../gaming-pc/) and [`k15/`](../k15/) are
canonical.** This guide covers what the scripts can't: the physical build, the
one-time OS/TV/Steam settings, the registration commands, and the failure
drills that prove it. Where a stage needs a script, it names the file rather
than reproducing it.

**Starting state:** gaming PC and TV set up and working normally. Voice control
is a separate overlay — do this guide first, then
[voice-testing.md](voice-testing.md).

**Design decisions this build rests on:** every Windows logon unconditionally
restores OFFICE mode (no boot-gating flag); SSH is signaling only — all
display/Steam work runs via interactive Scheduled Tasks; the TV powers on early
(EDID needs to be live) but switches input **last**; and the Puck/VirtualHere
handoff is validated before any automation is written, because it is the one
real architectural unknown.

---

## Stage 0 — Conventions (10 min, at any keyboard)

All values below are the real ones for this build — every command and script
already uses them; nothing needs substituting.

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

**Where code lives on each machine** — the two halves deploy differently:

| Machine | Location | How to update |
|---|---|---|
| Gaming PC | `C:\CouchGaming\` with a `logs\` subfolder | **`gaming-pc\Deploy.ps1`** ships the scripts there and stamps `build-id`; the runtime also needs gitignored binaries (`vhui64.exe`) and shortcuts (`OFFICE.lnk`, `TV-GAMING.lnk`) that can't live in the repo |
| K15 | a git clone on the Desktop, e.g. `%USERPROFILE%\Desktop\slopstation\k15\` with `logs\` and `state\` subfolders | **`git pull` in place** |

Every script derives its sibling paths from its own location, so each folder is
a relocatable unit. Per-machine files are gitignored and created once from
committed examples (`config.json` ← `config.example.json`, `secrets.json` ←
`secrets.template.json`) so a checkout runs without local config ever fighting
`git pull`.

---

## Stage 1 — Physical install & network (1–2 h)

1. **Switch:** place the 2.5G switch near the gaming PC/router. Uplink: one Cat6
   from a Deco LAN port → switch. Gaming PC Ethernet → switch (this link should
   negotiate 2.5G if the PC NIC supports it — check later in adapter status).
2. **Long Cat6 run:** switch → TV room, routed around the doorway. This and the
   HDMI can share a raceway.
3. **Long HDMI run:** the optical cable is **directional** — the end marked
   *Source* (or *1*) plugs into the RTX 4090's single HDMI port; *Display* (or
   *2*) into the TV's **HDMI 4**. Respect the minimum bend radius; optical
   cables die at kinks, not at length.
4. **Ultrawide:** if it's currently on the 4090's HDMI port, move it to
   DisplayPort now (the 4090 has exactly one HDMI 2.1 out and the TV needs it).
5. **K15 placement:** in the TV room, near the TV. Connect Cat6 and power. Leave
   USB free for the Puck and the SH-U35B.
6. **SH-U35B:** USB end → K15, 3.5 mm end → the TV jack marked **EX-LINK**. It's
   control-only — it never touches the video path.
7. **Puck:** stays boxed until Stage 4. First pairing happens at the gaming PC.
8. **Deco app:** create DHCP reservations for the gaming PC and the K15 at the
   addresses in Stage 0.

---

## Stage 2 — K15 first boot: make it an appliance (1 h)

The K15 has three jobs — orchestrator, VirtualHere host, media server — and one
requirement: it's *always* available.

1. Temporarily plug the K15 into a spare TV input (or any monitor) with a short
   HDMI for setup. Complete Windows 11 OOBE with a **local account** (simplest
   for a headless box), hostname `K15`.
2. Windows Update fully; install the FTDI VCP driver if the SH-U35B doesn't
   enumerate on its own (check Device Manager → Ports).
3. **Power settings:** Settings → System → Power → Never sleep, never turn off
   (plugged in). Disable hibernate too: `powercfg /h off` (elevated).
4. **Auto-logon:** run `netplwiz`, untick "Users must enter a user name and
   password" (or use Sysinternals Autologon). The orchestrator and listeners run
   in the user session, so the K15 must land on the desktop unattended after
   power loss.
5. **Remote management:** Settings → System → Remote Desktop → On. From now on,
   manage the K15 over RDP; the short HDMI can stay connected to a spare TV
   input as a maintenance path or come out.
6. Install Python 3.12+ (python.org installer, "Add to PATH" checked), then:
   `pip install pyserial hidapi`. Use `hidapi`, **not** the similarly-named
   `hid` package — `hid` is bindings-only and fails at import with a
   missing-DLL error; `hidapi` bundles the native library.
7. Clone this repo to the Desktop and create the per-machine config:

   ```
   cd %USERPROFILE%\Desktop
   git clone <repo-url> slopstation
   copy slopstation\k15\config.example.json slopstation\k15\config.json
   ```

8. Verify: from the gaming PC, `ping K15` works; from the K15,
   `ping 192.168.68.67` works.

---

## Stage 3 — Prove the video path + fix TV behavior (1–2 h, mostly soak time)

No software yet. A flaky cable produces symptoms identical to broken scripting
later — eliminate that first.

### 3.1 TV input settings (do these before judging picture)

1. **Settings → Support → About This TV** — record the exact model code (e.g.
   QN77S90CAFXZA).
2. **Input Signal Plus → ON for HDMI 4** (Settings → All Settings → Connection →
   External Device Manager → Input Signal Plus). Without this the port won't run
   full-bandwidth 4K120 RGB.
3. On the Home/Connected Devices screen, **edit HDMI 4's name/icon to "PC"** —
   this enables proper 4:4:4 chroma handling.
4. **Game Mode: On/Auto** (under Game Mode Settings / External Device Manager —
   exact menu location varies slightly by firmware).
5. **Disable automatic HDMI source switching.** Update TV firmware first. Then,
   with the TV on the *TV/live* source (not an HDMI input), press on the Samsung
   remote: **Mute → Volume Down → Channel Down → Mute**. There's no confirmation
   UI. Verify afterwards: power the PC on/off a few times during 3.2 and confirm
   the TV never jumps inputs on its own. Leave **Anynet+ (CEC) on** — the Apple
   TV uses it, and the PC path doesn't (GPUs have no CEC, which is also why
   turning the TV on can never affect the PC).

### 3.2 Video soak test

Manually (Windows Display Settings on the gaming PC, TV on HDMI 4):

1. Duplicate/extend to the S90C, then set it to **3840×2160 @ 120 Hz, SDR**.
   Play games for 30–60 min. Watch for blackouts, flashes, link drops.
2. Then enable **VRR / G-Sync Compatible** (NVIDIA Control Panel → Set up
   G-SYNC). Soak again.
3. Then enable **HDR** (Windows HD Color). Soak again.
4. While you're here, note the TV's EDID name as Windows reports it (Display
   Settings, or `Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID`) —
   Stage 8's detection check uses it.
5. Optional stretch goal, later: 144 Hz. 120 is the deliberately boring
   compatibility target for cable + HDR + VRR; revisit only after weeks of
   stability.

**If the link is flaky:** reseat both ends (verify Source/Display orientation),
drop to 60 Hz to distinguish bandwidth from link problems, and test the cable on
a short run before blaming anything else. Do not proceed to automation with a
marginal cable.

When done, set the ultrawide back as the only display for now.

---

## Stage 4 — Controller pairing, then the VirtualHere go/no-go (1–2 h) ⚠️ architectural gate

This is the most important experiment in the project. VirtualHere forwards raw
USB, so the gaming PC *should* see the literal Puck and Steam *should* treat the
controller as fully native — but nobody documents the 2026 Puck specifically.
Prove it before writing a line of automation.

### 4.1 Pair + firmware at the gaming PC (local, no network tricks)

1. Snap the Puck onto the controller magnetically.
2. Plug the Puck into the **gaming PC** with the bundled USB-C cable, open Steam.
3. Follow the prompts to update Puck firmware, then controller firmware (Steam
   will ask you to move the USB-C between Puck and controller mid-process).
4. Verify everything native and local: trackpads, gyro, grip buttons
   (L4/L5/R4/R5), haptics, Steam Input config.
5. Leave the controller in **Puck mode** — don't set up Bluetooth (higher
   latency, and it would add a manual mode-toggle to every session).

### 4.2 VirtualHere install — server on the K15

1. On the **K15**, download the VirtualHere Windows **server** —
   `vhusbdwinw64.exe` (Intel/AMD 64-bit) — into a stable folder.
2. Double-click it once and confirm it runs (tray icon appears; the hub becomes
   visible from the client in 4.3).
3. Make it survive reboots. Preferred: install it as a service from an **admin**
   terminal:

   ```
   .\vhusbdwinw64.exe -b
   ```

   Verify with `Get-Service *virtualhere*` → Status should be `Running`. If `-b`
   complains, check `.\vhusbdwinw64.exe --help` for the current service flag —
   or skip the service and drop a shortcut to the exe in the Startup folder
   (Win+R → `shell:startup`), which is equivalent here since Autologon
   guarantees a session.
4. No settings need changing; as a service it runs headless and its defaults are
   correct. (Licensing: the free server shares exactly **one** USB device — the
   Puck is that device, and since one Puck supports up to 4 controllers, even
   future multiplayer stays free.)

### 4.3 VirtualHere install — client on the gaming PC

1. On the **gaming PC**, download the VirtualHere **client** (`vhui64.exe`) into
   `C:\CouchGaming\` and run it.
2. The client is a single small window, entirely right-click driven. Right-click
   the root **USB Servers** line → tick **Start minimized**. If the menu has no
   start-at-boot option, add a shortcut to `vhui64.exe` in the Startup folder.
3. Same right-click menu → **Specify USB Server…** → enter `192.168.68.75:7575`.
   A directly-specified hub reconnects via aggressive direct TCP after the PC
   wakes from sleep, ~10 s faster than waiting for Auto-Find's broadcast
   discovery (measured: the Enter script's "VirtualHere sees Puck" gate dropped
   from ~15 s to ~5 s on the wake path). Leave Auto-Find on as a fallback.
4. You'll see every USB device the K15 is sharing — including ones you must
   **never claim**:
   - **FT232R USB UART** = the SH-U35B serial cable. It must stay local to the
     K15 (it's the TV-control COM port). Being listed is harmless; claiming it
     would break Ex-Link.
   - Any internal radio (may appear as **Wireless_Device** or similar) = the
     K15's own Bluetooth. Leave it alone.
5. Right-click **USB Servers** → **Advanced Settings** and tune reconnect timing
   for the wake-from-sleep path (apply by exiting and relaunching the client):
   **Ping tab** → Server Ping Period `2`, Server Ping Timeout `5` (faster
   dead-socket detection after resume; don't go tighter — this same ping is the
   in-game liveness check). **Lookup tab** → Look for new servers every `5` (the
   default 30 s cycle is the main source of reconnect variance after wake).
   Leave the Auto-Use tab untouched.
6. **Never enable Auto-Use or Auto-Use All Devices.** Auto-claim would steal the
   Puck whenever the PC is awake at your desk, deafening the couch trigger — and
   would grab the serial cable too. Every claim in this system is explicit, made
   by the scripts.

### 4.4 The go/no-go test

1. Move the Puck from the gaming PC to a **K15 USB-A port**.
2. On the gaming PC, a new device appears in the client under the K15 hub.
   Confirm it's the Puck: right-click → Properties → vendor ID **28DE** (Valve).
3. Get its address from an ordinary terminal on the gaming PC:

   ```
   C:\CouchGaming\vhui64.exe -t "LIST"
   ```

   The Puck's address is **`K15.5`** — the scripts already use it. If it ever
   changes (e.g. after a firmware update), `LIST` is the source of truth.
4. Claim it:

   ```
   C:\CouchGaming\vhui64.exe -t "USE,K15.5"
   ```

   Windows plays the device-connect chime; Steam shows a Steam Controller
   connecting. (Right-click → **Use** in the client window does the same thing —
   the command line is shown because the scripts use it.)
5. In Steam, verify **everything** again, now through the wall: trackpads, gyro,
   grips, haptics, rumble, Steam Input remapping.
6. Play something twitchy for 20–30 minutes and judge latency honestly. Wired-LAN
   USB forwarding adds ~1–2 ms; it should feel indistinguishable from 4.1.
7. Release it:

   ```
   C:\CouchGaming\vhui64.exe -t "STOP USING,K15.5"
   ```

   The device should return to the K15 within a few seconds.
8. Repeat the claim/release cycle **five more times**. It must be boring.

### 4.5 Verdict

**GO:** Steam sees a native Steam Controller with full functionality through
VirtualHere → the architecture is green-lit; everything after this stage is
plumbing.

**NO-GO:** the Puck misbehaves under forwarding (missing inputs, disconnects,
laggy feel) → stop and reassess the controller path before building any
automation. Fallbacks, in order: Bluetooth pairing direct to the gaming PC
through the wall (works, but worse latency), or a USB-over-Cat6 extender for the
Puck (new hardware). Don't build the automation on a red gate.

---

## Stage 5 — Ex-Link TV control from the K15 (30–60 min)

Samsung's jack transmits on tip / receives on ring; the SH-U35B is Tip=RXD,
Ring=TXD from the adapter side — a straight match, no crossover needed.

1. On the K15, Device Manager → Ports → note the **USB Serial Port (COMx)**
   number, and set `tvComPort` in `k15\config.json` to match.
2. Serial parameters, already in the code: **9600 baud, 8N1, no flow control**
   (community-established for consumer Ex-Link; 115200 on these ports is the
   debug console, not control).
3. Validate with [`k15/exlink.py`](../k15/exlink.py), in this order, from the
   `k15` folder:
   - TV **on**, watching anything: `python exlink.py power_off` → TV turns off.
   - TV in **standby**: `python exlink.py power_on` → TV turns on. The port is
     powered in standby on native-Ex-Link models. If — and only if — power-on
     from standby fails, check the TV's eco/power-saving settings.
   - TV on any source: `python exlink.py hdmi4` → discrete jump to HDMI 4, no
     source-menu cycling. Confirmed mapping on this set: `hdmi1` = Apple TV,
     `hdmi2` = PS5, `hdmi3` = eARC (soundbar), `hdmi4` = PC. Teardown returns to
     `hdmi1`.

   Every accepted frame acks with exactly `030cf1`, and `exlink.py` validates it
   — a `FAILED` line means the command really didn't land.

4. **Do not enter the Samsung service menu.** On native-port models it's
   plug-and-play; service-menu steps in older guides apply to USB-dongle models.
   If commands genuinely don't land after step 3: temporarily toggle Anynet+ off
   and retest (rare interference reports), and double-check you're in the
   EX-LINK jack, not a look-alike service jack.

**Frame format**, for reference: `08 22 c1 c2 c3 value` + checksum, where
checksum = `(0x100 - sum(first 6)) & 0xFF`. The table is frozen in
[`k15/cglib.py`](../k15/cglib.py) and cross-checked against the builder by
`voice/tests/test_exlink.py`, because **a one-byte slip in the volume family is
`power_off`**.

**Fallback (keep in your pocket, don't build now):** Tizen local WebSocket
control (`samsungtvws`) + SmartThings for discrete input selection, with network
wake ("Power On with Mobile"). Only needed if Ex-Link ever proves unreliable.

---

## Stage 6 — Display profiles + the OFFICE fail-safe (1 h)

### 6.1 Profiles (gaming PC)

Install **DisplayMagician** (latest stable, GitHub). Then:

**OFFICE**
1. TV off or on Apple TV; long HDMI stays connected.
2. Windows Display Settings → select the S90C → *Disconnect this display*
   (genuinely disabled, not secondary).
3. Ultrawide primary, native res/refresh; default audio = desk speakers/headset.
4. Save profile as `OFFICE`; create its permanent-switch shortcut at
   `C:\CouchGaming\OFFICE.lnk`.

**TV-GAMING**
1. TV on, HDMI 4 (use the remote for setup).
2. Enable the S90C, make it primary at **3840×2160 @ 120 Hz**; disable the
   ultrawide. HDR/VRR per your Stage 3 results.
3. Default audio = NVIDIA HDMI → S90C.
4. Save as `TV-GAMING`; shortcut at `C:\CouchGaming\TV-GAMING.lnk`.

Do **not** use extend, and don't use DisplayMagician's auto-rollback game
shortcuts — the session scripts own the lifecycle.

Both `.lnk` files are gitignored (machine-generated), so this step is required
on any fresh deploy.

Test: double-click each shortcut alternately **10 times**. Then reboot twice and
sleep/wake twice with the TV both on and off; ordinary use must always come back
ultrawide-only. Include one adversarial case: leave the TV powered on and
manually sitting on HDMI 4, reboot the PC, and confirm Windows still comes back
ultrawide-only.

### 6.2 The fail-safe: OFFICE at every logon, unconditionally

[`gaming-pc/Office-Safety.ps1`](../gaming-pc/Office-Safety.ps1) — verify-and-retry,
not fire-and-forget: on every normal boot it confirms office and exits instantly;
after a crash that left the TV-primary topology it applies OFFICE with up to 3
verified attempts (DisplayMagician's first post-boot launch is its slowest — a
single blind attempt can miss), and logs itself so unattended recoveries leave
evidence. It stands down while an Enter or Exit task is running.

Register it (elevated PowerShell on the gaming PC):

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' `
     -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\CouchGaming\Office-Safety.ps1'
$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$t.Delay = 'PT20S'
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'ForceOfficeAtLogon' `
     -Action $a -Trigger $t -RunLevel Highest
```

The invariant this buys: normal boot, update reboot, crash-and-restart, power
loss mid-game (where Windows would otherwise come back with the TV-primary
topology as last-used) — **all** converge to ultrawide-only at next logon, and
the task sends zero TV commands, so a TV that's off stays off and an Apple TV
night stays undisturbed. Couch launches aren't affected because the Enter task
(Stage 8) can only run *after* logon anyway.

Known gap the task cannot close: the Windows **login screen** renders before any
at-logon task and uses the last-used topology — after a hard kill mid-TV-session,
the sign-in prompt appears on the TV. Recovery: Win+P works on the login screen
(pick "PC screen only"), or sign in facing the TV; OFFICE converges ~20–60 s
after logon. Optional closure: auto-logon on the gaming PC (`netplwiz`), trading
physical-access security for never showing a prompt on the TV.

### 6.3 The wake fail-safe: cleaning up abandoned sessions

Resume-from-sleep is not a logon, so `ForceOfficeAtLogon` never fires on it — and
a session abandoned without running End TV Session (quit game, walk away, PC
idle-sleeps still in TV mode) would otherwise wake into a dark desk with a stale
Puck claim (VirtualHere re-acquires the device on reconnect, creating a fresh
instance while Steam holds handles to the old one — controller haptics work,
inputs dead).
[`gaming-pc/Wake-Safety.ps1`](../gaming-pc/Wake-Safety.ps1) closes the gap.

Register on the resume event (elevated PowerShell):

```powershell
schtasks /Create /TN "\CouchGaming\WakeSafety" /SC ONEVENT /EC System /MO "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\CouchGaming\Wake-Safety.ps1"
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'WakeSafety' -Settings $s
```

Semantics: keyboard wake into a stale session → full Exit cleanup (desk restored,
Puck released fresh — which also cures the stale-claim controller); normal desk
wake → no-op; chord/WoL wake → stands down so Enter owns it. The network-wake
match keys on `powercfg /lastwake` text — verify your NIC's wake string matches
`$NetworkWakePattern` after the first WoL wake (the transcript prints it raw) and
widen the pattern if needed. The K15 needs no changes for abandoned sessions: its
watch loop already declares the session dead ~20 s after the PC sleeps, powers
the TV off, and re-arms the chord.

### 6.4 One sign-in setting

Settings → Accounts → Sign-in options → "If you've been away, when should Windows
require you to sign in again?" → **Never**. Resume-from-sleep must land on the
desktop, not the lock screen, or the couch flow wakes the PC into a locked
session and Big Picture ends up behind it. (Home-LAN tradeoff; if the machine
must lock, the alternative is treating "locked" as a launch-abort condition,
which degrades the console UX.) **Sleep is the couch-ready state.**

---

## Stage 7 — Wake-on-LAN (30 min)

1. BIOS/UEFI: enable *Wake on LAN / Power On by PCI-E*. While there, disable
   ErP/deep-sleep states that cut standby power to the NIC.
2. Windows Device Manager → Ethernet adapter → Power Management: *Allow this
   device to wake the computer* + *Only allow a magic packet*. Advanced tab:
   *Wake on Magic Packet: Enabled*.
3. Confirm `powercfg /a` shows Standby (S3) or Modern Standby available; put the
   PC to **sleep** (not shutdown).
4. From the K15, send a magic packet and confirm the PC wakes. Repeat 5×,
   including once after the PC has slept overnight. Do not proceed until it's
   boring.

---

## Stage 8 — SSH signaling + interactive session tasks (1–2 h)

SSH is the secure RPC channel; the actual display/Steam/USB work runs inside your
logged-in desktop session via Scheduled Tasks — GUI and display-topology
operations are unreliable from SSH's non-interactive session, so the SSH account
is allowed to do exactly one thing: kick tasks.

### 8.1 OpenSSH Server on the gaming PC (elevated PowerShell)

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-NetFirewallRule -Name sshd-k15 -DisplayName 'OpenSSH from K15 only' -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress 192.168.68.75 -Action Allow
Get-NetFirewallRule -Name *OpenSSH* | Disable-NetFirewallRule   # drop the default any-source rule
```

### 8.2 Key + forced command

On the **K15**: `ssh-keygen -t ed25519` (no passphrase, it's an automation key),
then copy the contents of `%USERPROFILE%\.ssh\id_ed25519.pub`.

On the **gaming PC** — your account is an Administrator, so Windows sshd reads
keys from `C:\ProgramData\ssh\administrators_authorized_keys` (this path trips
everyone up). Create that file containing exactly one line — the forced command,
the restrictions, then your public key:

```
command="powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Dispatch.ps1",no-port-forwarding,no-agent-forwarding,no-x11-forwarding ssh-ed25519 AAAA...your-key... minipc@K15
```

Then fix its ACL or sshd will ignore it:

```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r `
  /grant "SYSTEM:F" /grant "BUILTIN\Administrators:F"
```

[`gaming-pc/Dispatch.ps1`](../gaming-pc/Dispatch.ps1) is **the entire remote
attack surface**: seven verbs — `enter` / `exit` / `status` / `games` /
`playing` / `launch <appid>` / `version` — and everything else answers
`DENIED`. It is deliberately dependency-free (no dot-sourcing in the sshd
context).

On the K15, `%USERPROFILE%\.ssh\config`:

```
Host gamepc
  HostName 192.168.68.67
  User tillm
  IdentityFile ~/.ssh/id_ed25519
  ConnectTimeout 5
```

Test from the K15: `ssh gamepc enter` should print `OK` (the task will
no-op-fail until 8.4 — fine), `ssh gamepc status` prints `NOTREADY`,
`ssh gamepc games` returns JSON, `ssh gamepc whatever` prints `DENIED`, and
`ssh gamepc` with a shell attempt gets the dispatcher, not a prompt.

### 8.3 Host session scripts

Deploy the script set to `C:\CouchGaming\` — from the repo checkout, on the
PC (this is also how every later update ships; it stamps a `build-id` the
K15's doctor compares against its own checkout):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\gaming-pc\Deploy.ps1
```

[`Enter-TV.ps1`](../gaming-pc/Enter-TV.ps1),
[`Exit-TV.ps1`](../gaming-pc/Exit-TV.ps1),
[`Launch-Game.ps1`](../gaming-pc/Launch-Game.ps1) and the safeties all
dot-source [`CouchGaming.common.ps1`](../gaming-pc/CouchGaming.common.ps1),
which holds every machine-specific value and the shared primitives. The design
choices baked into them, because each one cost a debugging session:

- **Condition-polled waits with elapsed-time stamps**, never blind sleeps.
- **Display state is read from a fresh process** (`GetSystemMetrics` via an
  encoded child command): WMI display classes and in-process metrics both report
  stale values inside windowless scheduled tasks.
- **VirtualHere results are redirected to a file with `-r`** — console-less `-t`
  calls otherwise throw GUI popups.
- **Claims and releases are verified by Windows device enumeration**, not by
  VirtualHere's IPC report, which can read `FAILED: API Timeout` on an operation
  that actually succeeded.
- **An immediate client nudge plus a hub-reconnect gate** for the
  wake-from-sleep path: the client's TCP link dies during S3 sleep, and claiming
  before reconnect returns `ERROR: Invalid address`.
- **The Puck claim is parallelized with profile settling** — profile
  verification happens after the USB phase, since it had that whole window to
  take.
- **Big Picture is forced to the foreground on enter**, and closed **while still
  on the TV** as exit's first act, so Steam's window never gets
  resolution-yanked mid-render.
- **DisplayMagician is killed after every apply**, verified or not: a lingering
  instance is what produces the frozen profile window on the next apply.

Measured: warm enters ~6–8 s, wake-from-sleep enters ~8–13 s to READY, dominated
by S3 resume + VirtualHere reconnect.

Reading the transcripts (`C:\CouchGaming\logs\`): a healthy enter ends `READY`, a
healthy exit ends `Puck released`. A line like `vh attempt 1: FAILED: API Timeout
3 sec` immediately after a successful `Puck enumerated` gate is documented
cosmetic noise — the first IPC call after a display switch often times out on the
report even when the operation succeeded; the enumeration checks are the source
of truth.

### 8.4 Register the tasks (interactive session, on demand)

```powershell
foreach ($n in 'Enter','Exit') {
  $a = New-ScheduledTaskAction -Execute 'powershell.exe' `
       -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\CouchGaming\$n-TV.ps1"
  $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
  Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName $n -Action $a -Settings $s
}
```

And the launch task, used by the `launch <appid>` verb:

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Launch-Game.ps1'
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'LaunchGame' -Action $a -Settings $s
```

And the voice-navigation tasks, used by the `nav`/`stop` verbs (same interactive
session, same 5-minute limit — `nav` forwards a `steam://` URL into Big Picture,
`stop` quits the running game and re-focuses Big Picture):

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Nav-BigPicture.ps1'
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Nav' -Action $a -Settings $s

$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\CouchGaming\Stop-Game.ps1'
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'StopGame' -Action $a -Settings $s
```

The 5-minute execution limit is load-bearing: Task Scheduler ignores start
requests for a task that is "currently running," so any hung instance would
otherwise silently kill every future run — the tile keeps reporting success while
nothing happens. The limit makes a wedge self-clear.

`-RunLevel Highest` is deliberately absent: neither script needs elevation, and
an elevated task can't be started by non-elevated Steam — the End TV Session tile
would fail with Access denied.

```powershell
# already registered with -RunLevel Highest and/or no time limit? Fix in place:
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Exit'  -Settings $s
Set-ScheduledTask -TaskPath '\CouchGaming\' -TaskName 'Enter' -Settings $s
```

Registering as the current user with no stored credentials makes them **run only
when you're logged on, in your interactive session** — exactly what display
switching and Steam need. `schtasks /Run` on them fails if no session exists yet,
which the K15 treats as "not ready, retry" — that's the mechanism that serializes
couch launches *after* logon and after `ForceOfficeAtLogon`.

Test locally at the PC: run the Enter task from Task Scheduler with the TV on →
TV-only + Puck claimed + Big Picture. Run Exit → office restored, Puck released.
Ten times. Check the transcripts in `logs\` for anything ugly.

Then run [`Doctor.ps1`](../gaming-pc/Doctor.ps1) — it checks every file, task,
sshd/firewall/key ACL, VirtualHere, the display probe, and session state in one
read-only pass.

---

## Stage 9 — K15 orchestrator (1–2 h)

Set the values in `k15\config.json` (created in Stage 2 from
[`config.example.json`](../k15/config.example.json)): `gamingPcMac`,
`gamingPcIp`, `sshHost`, `tvComPort`, `tvGamingCmd`, `tvIdleCmd`, and
`tvOffWhenDone` (`true` = the TV powers off after sessions; `false` = it returns
to the Apple TV on `tvIdleCmd`).

[`k15/couch.py`](../k15/couch.py) is the orchestrator. Run it by hand once:

```
python couch.py start
```

Sequencing recap, because it *is* the product: the TV powers on **early** (so
Windows can see its EDID, and the viewer keeps watching whatever was on); WoL in
parallel; `enter` retries until your Windows session exists (which guarantees
`ForceOfficeAtLogon` already ran on a cold boot); the input switch to HDMI 4
happens **only after** the host writes READY — so the one visible transition
lands on a working Big Picture. **Any failure before READY leaves the TV exactly
as it was.**

The watch loop polls every 5 s, so the TV returns to Apple TV within seconds of
session end. It is also the end-of-session detector: the Exit task removes the
`ready` marker (or the PC sleeps and SSH dies), the K15 notices, restores the TV,
and unlocks for the next launch. The session lock carries a heartbeat, so a lock
whose owner died goes stale and is recycled after 5 minutes rather than blocking
launches forever; `couch.py reconcile` re-adopts or clears a lock that survived a
K15 restart.

Every line `couch.py` prints is also appended to `couch.log` beside it, so
chord-launched runs — whose console closes with them — always leave a trail.

Then run [`k15/doctor.py`](../k15/doctor.py) for the read-only chain diagnosis:
config, deps, Ex-Link port, Puck, listener, haptics, the ssh contract, and
session state.

---

## Stage 10 — Visible launcher + failure drills (1 evening)

1. **K15 launcher** — [`k15/Start-TV-Gaming.bat`](../k15/Start-TV-Gaming.bat)
   runs `couch.py start` in a window that stays open so the log is readable.
   Keep a shortcut to it on the Desktop forever as the manual recovery trigger,
   even after the chord exists.

   **"End TV Session" tile in Big Picture** — do NOT add a `.bat` to Steam
   directly; bare `.bat`/`.lnk` adds silently fail to appear in the library. The
   reliable method is adding a real exe with launch options:
   - Steam (desktop) → **Add a Game → Add a Non-Steam Game → Browse** → paste
     `C:\Windows\System32\schtasks.exe` in the File name box → Open → confirm
     **schtasks** is checked → **Add Selected Programs**.
   - Library → right-click it → **Properties** → Name: `End TV Session`,
     **Launch options:** `/Run /TN \CouchGaming\Exit` (no quotes — the path has
     no spaces, and unquoted leaves nowhere for smart-quote corruption to hide).
   - Test from desktop Steam once (brief console blink + fresh `exit-*.log`),
     then in Big Picture highlight the tile → ☰ menu button → **Add to
     Favorites**. It also self-pins via Recent Games since it runs every session.
   - **Desk hotkey for the same teardown:** gaming-PC desktop → New → Shortcut →
     location `C:\Windows\System32\schtasks.exe /Run /TN \CouchGaming\Exit` →
     name "Exit TV Mode" → Properties → Shortcut key **Ctrl+Alt+E**, Run:
     Minimized. Works blind from the desk keyboard regardless of which display is
     live (hotkey shortcuts must live on the Desktop or Start Menu to register).
   - Designed exit is quit-game-then-tile; pressing the tile with a game still
     running gets Steam's close-the-running-game prompt (or a no-op on some
     client builds), per Steam's one-title-at-a-time rule.

2. Run the happy path from all four starting states — the full matrix:
   - PC awake + TV on (Apple TV)
   - PC asleep + TV on (Apple TV)
   - PC awake + TV off
   - PC asleep + TV off ← the real acceptance scenario

   (When the PC is already awake at the desk, the couch trigger deliberately
   wins — it's explicit.)

3. **Failure drills** — each must leave the TV on its prior source and the office
   recoverable:
   - Pull the gaming PC's Ethernet, trigger a launch → K15 times out, TV never
     switches.
   - Rename the Enter task temporarily → `enter` never returns OK → clean abort.
   - Unplug the Puck from the K15, launch → Enter's claim fails → office
     restored, TV untouched.
   - Kill Steam mid-launch → Enter's catch block restores OFFICE.
   - Hard-reset the PC mid-game → next logon lands in OFFICE; K15 watch loop
     restores the TV.
   - Mash the launcher three times fast → one session, two "already active" logs.

4. Only after all drills pass, append a hard-sleep to the end of `Exit-TV.ps1` if
   you want the PC to doze immediately after sessions:
   `Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)`

Live with the visible launcher for **a week** before Stage 11.

---

## Stage 11 — The controller chord

The trigger: **hold Steam + right-trigger (full pull) for 2 s** while idle. Valve
doesn't document the controller's HID report format, so this stage is
calibrate-then-wire. The Puck is local to the K15 whenever a session isn't
running — that's when the chord is audible.

### 11.1 Calibrate the report bytes

[`k15/calibrate.py`](../k15/calibrate.py) learns which bytes are sensor noise
during a 3 s hands-off window, filters to the dominant report type, then prints
only meaningful changes. Run it with the controller awake and flat, press Steam
alone, then the right trigger alone, and record the (byte index, bit) each
toggles.

**Measured on this controller:** both digital bits live in `byte[4]` — Steam =
`0x01`, right-trigger full-pull click = `0x80` (bytes 8/9 are the trigger's
analog travel; ignore them), and the input report type is `0x42`. If a firmware
update ever shuffles the layout, the chord goes quiet and re-running this
calibration — then updating `RID_INPUT` / `BTN_BYTE` / `CHORD` in
[`chord_listener.py`](../k15/chord_listener.py) — is the five-minute fix.

### 11.2 The listener

[`k15/chord_listener.py`](../k15/chord_listener.py) holds the Puck's HID
interfaces open through controller sleep, so a chord from a cold controller is
heard the moment the controller reconnects (~1 s). Individual unreadable
interfaces are culled quietly (the Puck exposes ~13 and some error on read), and
it latches onto the input interface **by content** — only reports of the
hardcoded type `0x42` count.

**Never auto-learn the report type at startup:** multiple interfaces stream
different report types and whichever answers first wins a race (a status stream,
type `0x79`, was observed winning — chord deaf until restart). "Device vanished"
fires only when the pool empties or the latched input interface dies, which is
exactly the VirtualHere claim, i.e. the session boundary. `couch.py`'s session
lock guards against double-fires.

The listener answers through the controller's haptics — 1 thud = launching, 2 =
busy, 3 = the launch failed — mirroring the earcon counts on the voice side.

Lifecycle test: chord-started session → End TV Session tile → listener re-arms on
Puck return → second chorded session, hands never touching the K15. To force the
controller asleep for the cold-start case, hold the Steam button ~5 s.

### 11.3 Install at startup

[`k15/Start-K15.bat`](../k15/Start-K15.bat) is **the** Startup-folder shortcut
target and the one thing to run after a `git pull` — it starts whichever lanes
are down and reloads whichever are up. It launches
[`Start-Listener.bat`](../k15/Start-Listener.bat) (chord lane: `couch.py
reconcile` once, then the listener in a restart loop) and, once voice is set up,
`voice/Start-Voice.bat`.

The command that installs the Startup shortcut is in
[voice-testing.md § Autostart](voice-testing.md), since it covers both lanes.

Verify the unattended chain: reboot the K15 (or pull its power), touch nothing,
RDP in — the supervisor consoles are on the desktop; tap Steam on the controller
→ `armed` prints → chord starts a session. Keep the consoles minimized, not
closed (closing a window is the off switch for that lane), and keep the standing
RDP rule: disconnect, never sign out.

---

## Stage 12 — Media server (independent; any time after Stage 2)

SMB → Infuse, no Jellyfin. All on the K15 over RDP except steps 4–5.

1. **Folders + naming.** Create `C:\Media\Movies` and `C:\Media\TV`. Naming
   drives Infuse's metadata matching: movies as `Title (Year).mkv` (one file or
   one subfolder per movie); TV as `Show\Season 01\Show S01E01.mkv`. (The K15 has
   two spare M.2 slots — when the library outgrows C:, a dedicated SSD changes
   only the paths.)
2. **Dedicated read-only user** (admin PowerShell):

   ```powershell
   net user media "PickAPassword1" /add /passwordchg:no
   wmic useraccount where "name='media'" set PasswordExpires=false
   ```

3. **Permissions + share** (read-only for `media` — the Apple TV can't delete
   anything; full for `minipc` — how you drop files):

   ```powershell
   icacls "C:\Media" /grant "media:(OI)(CI)RX"
   New-SmbShare -Name Media -Path C:\Media -ReadAccess "K15\media" -FullAccess "K15\minipc"
   ```

   Confirm the K15's network profile is **Private** (Settings → Network →
   Ethernet) and file/printer sharing is on for Private profiles — sharing is
   blocked on Public.
4. **Infuse on the Apple TV:** Settings → Add Files → Other → **SMB** → Address
   `192.168.68.75`, user `media` + password → pick the `Media` share → Favorite
   it → let it scan. Playback is direct-play; the K15 only serves bytes.
5. **Dropping files from the gaming PC:** Explorer → `\\192.168.68.75\Media` →
   credentials `minipc` (tick Remember); optionally map it as a drive letter.
   Copies run at 2.5 Gb.
6. **Acceptance:** gaming PC asleep → Infuse plays. Media must be fully
   independent of the gaming stack.

Add Jellyfin later only if a concrete want appears (cross-device watch state,
user profiles, phone/web playback, remote access): install it on the K15, point
libraries at these same folders, connect Infuse in Direct Mode; the 125U's
QuickSync covers any transcoding. Jellyfin is never a prerequisite for anything
gaming-related.

---

## Stage 13 — Voice (optional overlay)

Once the chord works end to end, [voice-testing.md](voice-testing.md) is the
bring-up path: API keys, the voice venv, audio devices, wake word, then
escalating drills from a safe dry run to live dispatch.

Voice is an overlay, never load-bearing — the chord listener is a separate
process and survives anything the voice stack does.

---

## Acceptance checklist

**Isolation**
- [ ] Desk boot/reboot/update-reboot: TV never powers on, never changes input,
      S90C stays disabled in Windows.
- [ ] Desk wake-from-sleep: same.
- [ ] Turning the TV on/off, or manually flipping it to HDMI 4: gaming PC does
      nothing.

**Session**
- [ ] From PC asleep + TV off: one trigger → Big Picture, controller live,
      without entering the office.
- [ ] TV stays on its prior source until Big Picture is ready; exactly one
      visible input transition.
- [ ] Steam reports a native Steam Controller through VirtualHere;
      trackpads/gyro/grips/haptics intact.
- [ ] "End TV Session" restores the ultrawide, releases the Puck to the K15,
      returns the TV to Apple TV (or off), then the PC may sleep.

**Failure**
- [ ] Failed wake / failed profile / failed Puck claim / failed Steam: TV input
      never stolen, office recoverable.
- [ ] Hard crash mid-game: next logon is OFFICE; K15 restores the TV within a
      minute.
- [ ] Trigger spam: one session.

**Media**
- [ ] Infuse plays from the K15 with the gaming PC asleep.

---

## Quick reference

| Thing | Where |
|---|---|
| Ex-Link frames | `08 22 c1 c2 c3 val + (0x100 − Σ)&0xFF`, 9600 8N1 · on `D4` / off `D5` / HDMI4 `08220A000503C4` · ack `030cf1` |
| VirtualHere claim/release | `vhui64.exe -t "USE,K15.5"` / `"STOP USING,K15.5"` |
| Remote surface | `ssh gamepc enter\|exit\|status\|games\|playing\|launch <appid>\|version` — nothing else exists |
| Fail-safe | `\CouchGaming\ForceOfficeAtLogon` — unconditional, sends no TV commands |
| Diagnosis | `python doctor.py` on the K15 · `Doctor.ps1` on the PC |
| The one rule | Nothing switches the TV to HDMI 4 before the host writes READY |
