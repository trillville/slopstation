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
}

$script:CgStopwatch = [Diagnostics.Stopwatch]::StartNew()

function Log($m) { Write-Host ("[+{0,5:n1}s] {1}" -f $script:CgStopwatch.Elapsed.TotalSeconds, $m) }

function Start-CgTranscript([string]$Tag) {
    Start-Transcript (Join-Path $CG.LogDir ("{0}-{1}.log" -f $Tag, (Get-Date -Format yyyyMMdd-HHmmss)))
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

# The ready marker is the cross-machine session API: Enter writes it last, the
# K15 switches the TV input only after seeing it, Exit/safeties remove it.
function Set-ReadyMarker {
    New-Item -ItemType Directory -Force (Split-Path $CG.ReadyMarker) | Out-Null
    Set-Content $CG.ReadyMarker (Get-Date).ToString('o')
}
function Clear-ReadyMarker { Remove-Item $CG.ReadyMarker -ErrorAction SilentlyContinue }
function Test-ReadyMarker  { Test-Path $CG.ReadyMarker }
