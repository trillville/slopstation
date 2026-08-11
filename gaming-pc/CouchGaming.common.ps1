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
        foreach ($k in $Fields.Keys) { $rec[$k] = $Fields[$k] }
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
function Clear-OldLogs([int]$Days = 30) {
    # Both streams age out together: transcripts (the narrative) and the
    # daily .jsonl (the milestones Alloy ships).
    Get-ChildItem $CG.LogDir -Include *.log, *.jsonl -File -Recurse `
            -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$Days) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# The ready marker is the cross-machine session API: Enter writes it last, the
# K15 switches the TV input only after seeing it, Exit/safeties remove it.
function Set-ReadyMarker {
    New-Item -ItemType Directory -Force (Split-Path $CG.ReadyMarker) | Out-Null
    Set-Content $CG.ReadyMarker (Get-Date).ToString('o')
}
function Clear-ReadyMarker { Remove-Item $CG.ReadyMarker -ErrorAction SilentlyContinue }
function Test-ReadyMarker  { Test-Path $CG.ReadyMarker }
