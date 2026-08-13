# CouchGaming.common.ps1 - shared library for the couch gaming session scripts.
# Dot-source from a sibling script:   . "$PSScriptRoot\CouchGaming.common.ps1"
# Every machine/build-specific value lives in $CG; the scripts contain only sequencing.

$CG = @{
    Root        = $PSScriptRoot
    LogDir      = Join-Path $PSScriptRoot 'logs'
    Vh          = Join-Path $PSScriptRoot 'vhui64.exe'
    VhResult    = Join-Path $PSScriptRoot 'logs\vh-last.txt'
    VhNudge     = Join-Path $PSScriptRoot 'logs\vh-nudge.txt'
    Puck        = 'K15.5'                  # VirtualHere address; `vhui64 -t LIST` is the source of truth
    PuckHwId    = 'VID_28DE&PID_1304'      # Valve Steam Controller Puck
    TvEdid      = 'QCQ90S'                 # S90C's EDID name as Windows reports it
    SteamWindow = 'Steam'                  # EXACT title of the desktop library
                                           # window - the one window a session
                                           # must never leave in the foreground
    BpmWindow   = 'Steam Big Picture Mode' # ...and the one it should
    TvHeight    = 2160                     # see Test-TvIsPrimary
    OfficeLnk   = Join-Path $PSScriptRoot 'OFFICE.lnk'
    TvGamingLnk = Join-Path $PSScriptRoot 'TV-GAMING.lnk'
    ReadyMarker = 'C:\ProgramData\CouchGaming\ready'   # cross-context state - deliberately not under Root
    TurnMarker  = 'C:\ProgramData\CouchGaming\turn'    # written by Dispatch, read here (schtasks can't pass args)
}

$script:CgStopwatch = [Diagnostics.Stopwatch]::StartNew()

# The K15's correlation id for whatever caused this run. Re-validated on read
# even though Dispatch's regex already enforced it: this file is on disk, the
# value ends up in a filename, and a second cheap check is worth more than the
# assumption that nothing else can ever write there. Absent or malformed = no
# correlation, never a failure - the session matters, the telemetry does not.
# Age-gated for the same reason the session lock is: the marker outlives its
# run (the LaunchGame task is not elevated, so it cannot reliably delete a
# file the elevated Dispatch context created - see Dispatch.ps1), and a logon
# hours later must not tag Office-Safety's events with a long-dead launch.
$script:CgTurnStaleSec = 300

function Get-CgTurn {
    try {
        $f = Get-Item $CG.TurnMarker -ErrorAction Stop
        if (((Get-Date) - $f.LastWriteTime).TotalSeconds -gt $script:CgTurnStaleSec) { return '' }
        $t = (Get-Content $CG.TurnMarker -ErrorAction Stop -Raw).Trim()
        if ($t -match '^[0-9a-f]{1,8}$') { return $t }
    } catch { }
    return ''
}

$script:CgTurn = Get-CgTurn
# Lane defaults to the script that dot-sourced us, so a script with no
# transcript (Office-Safety, Wake-Safety) still emits under a sensible label.
$script:CgLane = if ($MyInvocation.PSCommandPath) {
    [IO.Path]::GetFileNameWithoutExtension($MyInvocation.PSCommandPath).ToLower()
} else { 'pc' }

function Log($m) {
    Write-Host ("[+{0,5:n1}s] {1}" -f $script:CgStopwatch.Elapsed.TotalSeconds, $m)
}

# Structured twin of Log, for the MILESTONES a dashboard counts and an alert
# fires on. The transcript stays the narrative - it catches the lines nobody
# thought to instrument - and this catches the ones we did.
function Write-CgEvent {
    param([string]$Event, [hashtable]$Fields = @{}, [string]$Level = 'info')
    try {
        $rec = [ordered]@{
            ts      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
            level   = $Level
            env     = 'prod'
            service = 'gamepc'
            lane    = $script:CgLane
            event   = $Event
        }
        if ($script:CgTurn) { $rec.turn = $script:CgTurn }
        $rec.host = $env:COMPUTERNAME
        $rec.dur_ms = [int]$script:CgStopwatch.Elapsed.TotalMilliseconds
        # A caller field must never overwrite a key the emitter owns (those are
        # Loki labels, and a record that misdescribes itself is worse than a
        # missing one). Keep the value under a prefixed name rather than
        # dropping it. Same rule as events.py - no binding hazard here, because
        # fields arrive in a hashtable rather than as splatted parameters.
        $owned = @('ts','level','env','service','lane','event','host')
        foreach ($k in $Fields.Keys) {
            if ($owned -contains $k) { $rec["f_$k"] = $Fields[$k] }
            else { $rec[$k] = $Fields[$k] }
        }
        $file = Join-Path $CG.LogDir ("pc-{0}.jsonl" -f (Get-Date -Format yyyyMMdd))
        $line = ConvertTo-Json -InputObject $rec -Compress -Depth 4
        # AppendAllText with an explicit BOM-less encoder, NOT
        # `Add-Content -Encoding utf8`: on Windows PowerShell 5.1 that flag
        # means "UTF-8 with BOM", and the three bytes it puts in front of the
        # first '{' make that line unparseable JSON forever after. Verified on
        # 5.1.26100 - the file starts EF BB BF 7B.
        [IO.File]::AppendAllText(
            $file, $line + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }     # telemetry never costs a session
}

function Start-CgTranscript([string]$Tag) {
    $script:CgLane = $Tag
    New-Item -ItemType Directory -Force -Path $CG.LogDir -ErrorAction SilentlyContinue | Out-Null
    # The turn rides in the FILENAME too, so "which transcript belongs to the
    # 9pm launch" is answerable by looking at the folder, not by opening files.
    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    $name = if ($script:CgTurn) { "{0}-{1}-{2}.log" -f $Tag, $stamp, $script:CgTurn }
            else                { "{0}-{1}.log" -f $Tag, $stamp }
    Start-Transcript (Join-Path $CG.LogDir $name)
    Write-CgEvent "${Tag}_start"
}

function Wait-For([scriptblock]$Cond, [double]$TimeoutSec, [string]$What) {
    $end = $script:CgStopwatch.Elapsed.TotalSeconds + $TimeoutSec
    while ($script:CgStopwatch.Elapsed.TotalSeconds -lt $end) {
        if (& $Cond) { Log $What; return $true }
        Start-Sleep -Milliseconds 250
    }
    Log "TIMEOUT waiting for: $What"; return $false
}

# Display state must be probed from a fresh process: WMI display classes and
# in-process GetSystemMetrics both report stale values inside windowless
# scheduled tasks.
$script:CgProbe = @'
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); [DllImport("user32.dll")] public static extern int GetSystemMetrics(int n);'
[void][W.N]::SetProcessDPIAware()
[W.N]::GetSystemMetrics(1)
'@
$script:CgProbeEnc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script:CgProbe))

function Get-PrimaryHeight { [int](& powershell.exe -NoProfile -EncodedCommand $script:CgProbeEnc | Select-Object -Last 1) }

# THE topology sentinel for the whole system: primary-display height equals the
# TV's. Holds because the desk ultrawide's height differs from 2160. Revisit
# before ever pairing this rig with a 4K or 5K2K desk monitor (5120x2160 would
# read as "TV is primary" at every logon).
function Test-TvIsPrimary { (Get-PrimaryHeight) -eq $CG.TvHeight }

function Get-TvNames {
    Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue |
    ForEach-Object { -join [char[]]($_.UserFriendlyName | Where-Object { $_ -ne 0 }) }
}

# Windows device enumeration is the arbiter of claim state - VirtualHere's IPC
# report can read "FAILED: API Timeout" on operations that succeeded.
function Test-PuckPresent {
    [bool](Get-PnpDevice -ErrorAction SilentlyContinue |
           Where-Object { $_.InstanceId -match $CG.PuckHwId -and $_.Status -eq 'OK' })
}

# Console-less -t calls throw GUI popups unless redirected to a file with -r.
function Get-VhList {
    & $CG.Vh -t "LIST" -r $CG.VhResult | Out-Null
    Start-Sleep -Milliseconds 400
    (Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '
}

# A lingering DisplayMagician instance is what produces the frozen profile
# window on the next apply - kill after every verified apply.
function Stop-DisplayMagician {
    Get-Process DisplayMagician -ErrorAction SilentlyContinue | Stop-Process -Force
}

# Steam's own record of what is running; 0 = nothing. Same registry value the
# Dispatch `playing` verb answers from, so the PC cannot disagree with itself
# about whether a game is up.
function Get-RunningAppId {
    $id = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).RunningAppID
    if ($id) { [int]$id } else { 0 }
}

if (-not ('CG.Win' -as [type])) {
    # WindowByTitle rather than the obvious FindWindow(null, title): verified on
    # this hardware, FindWindow returns 0 for windows EnumWindows reports with
    # exactly that title. Steam's UI is CEF, drawn by steamwebhelper.exe - which
    # is also why the steam.exe MainWindowHandle the original guard used reads 0
    # and why that guard never fired. Enumerating is the thing that works.
    # Visible-only on purpose: closed to the tray there is nothing to put away.
    Add-Type -Namespace CG -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
delegate bool EnumProc(IntPtr h, IntPtr p);
public static string ForegroundTitle() {
    IntPtr h = GetForegroundWindow();
    if (h == IntPtr.Zero) return "";
    var sb = new System.Text.StringBuilder(300);
    GetWindowTextW(h, sb, 300);
    return sb.ToString();
}
public static IntPtr WindowByTitle(string want) {
    IntPtr hit = IntPtr.Zero;
    EnumWindows((h, p) => {
        if (!IsWindowVisible(h)) return true;
        var sb = new System.Text.StringBuilder(300);
        GetWindowTextW(h, sb, 300);
        if (sb.ToString() == want) { hit = h; return false; }
        return true;
    }, IntPtr.Zero);
    return hit;
}
'@
}

# THE window a couch session must never hand the controller to. Steam delivers
# input to the FOCUSED window, so a session holding the desktop library window
# plays a navigation sound per button press and moves nothing on the TV - and
# the Steam button still works, because Steam Input handles that one globally.
# That combination is what makes the failure so baffling from the couch.
#
# Targeted by EXACT window title, NOT by the steam process's MainWindowHandle
# the way the original repaint guard did. That is why the original never once
# fired: Steam closed to the tray reports MainWindowHandle = 0, and across
# every exit in the logs the 'desktop Steam minimized' line is simply absent.
# An exact title match also cannot hit 'Steam Big Picture Mode', so unlike the
# handle version this is safe to call at any point in a session.
#
# Deliberately the same window AppActivate('Steam') used to focus: the thing
# that stole the controller is now the thing that gets put away.
#
# Minimizing rather than merely not-focusing also keeps Exit's original intent:
# a library window laid out at 4K comes back garbled when reopened at the
# ultrawide's resolution.
function Hide-DesktopSteam {
    $h = [CG.Win]::WindowByTitle($CG.SteamWindow)
    if ($h -ne [IntPtr]::Zero) {
        [void][CG.Win]::ShowWindow($h, 6)      # 6 = SW_MINIMIZE
        Log 'desktop Steam minimized'
    }
}

# What is ACTUALLY in front, as a string, for the ready event to carry.
# Recorded rather than reasoned about: the dead-controller bug survived three
# sessions because every signal we had said success - `focused=True` was in
# the ready event while the controller was reaching nothing. One field naming
# the window that really has the foreground makes that failure self-evident in
# the first launch instead of the fourth.
function Get-ForegroundTitle { [CG.Win]::ForegroundTitle() }

# Steam's install path is the registry's business, not a hardcoded path.
# Shared with Launch-Game, which needs the same exe for the same -applaunch.
function Get-SteamExe {
    $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath -replace '/', '\'
    $exe = Join-Path $steam 'steam.exe'
    if (-not (Test-Path $exe)) { throw "steam.exe not found at $exe" }
    $exe
}

# NOTE: there is deliberately no Resume-Game here. One existed briefly and was
# reverted - see docs/troubleshooting.md and the plan in the same commit. It
# ran `-applaunch` on the already-running game just before the ready marker,
# which put the game's 2160p re-init inside the exact window where the K15 is
# still flipping the TV input: black screen, dead controller, ~40 s. Enter is
# structurally the wrong place for it, because Enter cannot observe when the TV
# actually goes live - only the K15 can.

# Apply a DisplayMagician profile shortcut, poll $Until to verify it took, and
# kill DisplayMagician after every attempt - verified or not.
function Invoke-DisplayProfile([string]$Lnk, [scriptblock]$Until, [double]$TimeoutSec = 20, [int]$Attempts = 1, [string]$What = 'profile applied') {
    for ($i = 1; $i -le $Attempts; $i++) {
        Start-Process $Lnk
        $ok = Wait-For $Until $TimeoutSec "$What (attempt $i)"
        Stop-DisplayMagician
        if ($ok) { return $true }
    }
    return $false
}

# Claim the Puck, verified by Windows enumeration. A pre-existing (stale) claim
# is released first for a fresh instance: VirtualHere re-acquires the device on
# reconnect, and Steam holding handles to the old instance yields a
# haptics-alive / inputs-dead controller.
function Request-PuckClaim {
    if (Test-PuckPresent) {
        Log 'stale Puck claim detected - releasing for a fresh instance'
        & $CG.Vh -t "STOP USING,$($CG.Puck)" -r $CG.VhResult
        if (-not (Wait-For { -not (Test-PuckPresent) } 6 'stale claim released')) {
            # Proceeding would let the claim gate pass on the stale, dead instance -
            # reproducing the exact inputs-dead controller this recycle exists to
            # prevent. A clean abort (TV untouched) beats a fake-successful launch.
            throw 'stale Puck claim would not release - aborting launch'
        }
    }
    $claimed = $false
    for ($i = 1; -not $claimed -and $i -le 2; $i++) {
        & $CG.Vh -t "USE,$($CG.Puck)" -r $CG.VhResult
        $claimed = Wait-For { Test-PuckPresent } 8 "Puck enumerated (attempt $i)"
        Log ("vh attempt {0}: {1}" -f $i, ((Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '))
    }
    if (-not $claimed) { throw 'VirtualHere claim did not produce a device after 2 attempts' }
}

# Release the Puck; retry until Windows enumeration agrees it's gone.
function Request-PuckRelease([int]$Attempts = 3) {
    $released = $false
    for ($i = 1; -not $released -and $i -le $Attempts; $i++) {
        & $CG.Vh -t "STOP USING,$($CG.Puck)" -r $CG.VhResult
        Start-Sleep 1
        Log ("vh attempt {0}: {1}" -f $i, ((Get-Content $CG.VhResult -ErrorAction SilentlyContinue) -join ' '))
        $released = -not (Test-PuckPresent)
        if (-not $released) { Start-Sleep 2 }
    }
    $released
}

# schtasks wrappers for the \CouchGaming\ task folder.
# English-locale match - the same assumption Wake-Safety's powercfg parse makes.
function Test-CgTaskRunning([string]$Name) {
    try {
        $out = schtasks /Query /TN "\CouchGaming\$Name" /FO LIST 2>$null | Out-String
        return [bool]($out -match 'Status:\s+Running')
    } catch { return $false }
}

function Stop-CgTask([string]$Name) { schtasks /End /TN "\CouchGaming\$Name" | Out-Null }

# Transcript retention: one file per enter/exit/wake/logon adds up forever.
# Called from Office-Safety (every logon) and Wake-Safety (every desk wake -
# the real cadence, since sleep-centric use makes logons rare).
function Clear-OldLogs([int]$Days = 30, [int]$ArchiveAfterDays = 2) {
    # Transcripts move to archive\ after a couple of days, and are deleted
    # from there at $Days. The move exists for the SHIPPER, not for tidiness:
    # Alloy tails every file its glob matches, and a finished transcript can
    # never be appended to again - only a currently-running script's can grow.
    # Keeping 100+ closed files in the glob meant 100+ pointless tailers, and
    # a tailer costs ~0.04% of a core whether or not its file ever changes -
    # measured by A/B on one live process, see the cost note in
    # alloy\config.alloy.example (which also records why the obvious way to
    # measure this returns a silent zero). 110 files was ~4.5% of a core to
    # watch files that were finished. Archiving keeps the live set to the
    # handful that can actually still be written to, and the files stay on
    # disk.
    # Both streams archive, for the same reason: pc-*.jsonl is one file per day
    # and only TODAY's can be appended to, so at 30-day retention it was 29
    # tailers (~1.2% of a core) polling files that are finished forever.
    $archive = Join-Path $CG.LogDir 'archive'
    New-Item -ItemType Directory -Force -Path $archive -ErrorAction SilentlyContinue | Out-Null
    # Wildcard path + -Include and NO -Recurse: enumerates this directory's
    # files only, so archive\ is never re-scanned and nothing moves twice.
    # (-Filter takes one pattern, which is why this is not -Filter.)
    Get-ChildItem (Join-Path $CG.LogDir '*') -Include *.log, *.jsonl -File `
            -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$ArchiveAfterDays) } |
        Move-Item -Destination $archive -Force -ErrorAction SilentlyContinue

    # Then retention over both folders and both streams.
    Get-ChildItem $CG.LogDir -Include *.log, *.jsonl -File -Recurse `
            -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$Days) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# The ready marker is the cross-machine session API: Enter writes it last, the
# K15 switches the TV input only after seeing it, Exit/safeties remove it.
#
# CONTENT is the launch's turn id when one rode in with the enter verb: the
# K15 treats a READY as verified only when it echoes the turn it sent, so a
# stale marker can no longer satisfy a NEW launch's poll (which used to
# switch the TV to a host still mid-Enter). No turn falls back to the
# timestamp, accepted as a legacy READY, so either side may deploy first.
function Set-ReadyMarker {
    New-Item -ItemType Directory -Force (Split-Path $CG.ReadyMarker) | Out-Null
    $stamp = if ($script:CgTurn) { $script:CgTurn } else { (Get-Date).ToString('o') }
    Set-Content $CG.ReadyMarker $stamp
}
function Clear-ReadyMarker { Remove-Item $CG.ReadyMarker -ErrorAction SilentlyContinue }
function Test-ReadyMarker  { Test-Path $CG.ReadyMarker }
