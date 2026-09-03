# Ship the gaming-pc script set from this checkout to the runtime folder
# (default C:\CouchGaming) as one checked set, and stamp `build-id` so the K15
# can ask what is running (`ssh gamepc version`, compared by doctor.py).
# test_turn.py drills the REPO's Dispatch.ps1, so a hand-copied deployed copy
# would drift undetected.
#
# Run from a checkout, on the PC:
#   powershell -NoProfile -ExecutionPolicy Bypass -File Deploy.ps1
#
# -WaitMinutes > 0 parks until no session owns the PC (what CD passes). The
# default 0 keeps the hand-run behaviour: warn and copy anyway.
#
# Does not copy itself. The gitignored runtime pieces (vhui64.exe, OFFICE.lnk,
# TV-GAMING.lnk) are warned about, never touched.
param([string]$Dest = 'C:\CouchGaming', [int]$WaitMinutes = 0)

$scripts = @(
    'CouchGaming.common.ps1', 'Dispatch.ps1', 'Doctor.ps1',
    'Enter-TV.ps1', 'Exit-TV.ps1', 'Launch-Game.ps1',
    'Nav-BigPicture.ps1', 'Stop-Game.ps1',
    'Office-Safety.ps1', 'Wake-Safety.ps1'
)

# Own copy of the emitter, like Dispatch.ps1's: this script cannot dot-source
# CouchGaming.common.ps1 - it is what ships it, and common's $CG.LogDir would
# point at this checkout instead of the runtime folder the shipper tails.
# test_event_names.py holds every $owned list equal to events._EMITTER_OWNED.
function Write-CgEvent([string]$Event, [hashtable]$Fields = @{}, [string]$Level = 'info') {
    try {
        $rec = [ordered]@{
            ts      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
            level   = $Level
            env     = 'prod'
            service = 'gamepc'
            lane    = 'deploy'
            event   = $Event
        }
        $rec.host = $env:COMPUTERNAME
        $owned = @('ts','level','env','service','lane','event','host')
        foreach ($k in $Fields.Keys) {
            if ($owned -contains $k) { $rec["f_$k"] = $Fields[$k] }
            else { $rec[$k] = $Fields[$k] }
        }
        $dir = Join-Path $Dest 'logs'
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $file = Join-Path $dir ("pc-{0}.jsonl" -f (Get-Date -Format yyyyMMdd))
        # BOM-less encoder: PowerShell 5.1's `-Encoding utf8` means WITH BOM,
        # and those three bytes before the first '{' break JSON parsing.
        [IO.File]::AppendAllText(
            $file, (ConvertTo-Json -InputObject $rec -Compress -Depth 4) + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }     # telemetry never costs a session
}

# A session owns the PC while the READY marker exists or an Enter/Exit task is
# still running - mid-Enter the marker is not written yet, and that is the
# window where a swapped script set would run half-old. Literal path for the
# same reason Dispatch.ps1 uses one: no common.ps1 here.
$ready = 'C:\ProgramData\CouchGaming\ready'

function Test-SessionLive {
    if (Test-Path $ready) { return $true }
    foreach ($n in 'Enter', 'Exit') {
        $t = Get-ScheduledTask -TaskPath '\CouchGaming\' -TaskName $n -ErrorAction SilentlyContinue
        if ($t -and $t.State -eq 'Running') { return $true }
    }
    return $false
}

# Refuse a partial set - half a contract is skew.
$missing = $scripts | Where-Object { -not (Test-Path (Join-Path $PSScriptRoot $_)) }
if ($missing) {
    Write-Host "ABORT: this checkout is missing $($missing -join ', ') - nothing copied"
    exit 1
}

if (Test-SessionLive) {
    if ($WaitMinutes -le 0) {
        Write-Host 'WARNING: a session is live - copying anyway (pass -WaitMinutes to park instead)'
    } else {
        Write-CgEvent 'deploy_deferred' @{ reason = 'session_live'; budget_s = $WaitMinutes * 60 } 'warn'
        Write-Host "a session is live - waiting up to $WaitMinutes min"
        $deadline = (Get-Date).AddMinutes($WaitMinutes)
        while ((Test-SessionLive) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Seconds 15 }
        if (Test-SessionLive) {
            Write-CgEvent 'deploy_deferred' @{ reason = 'gave_up' } 'warn'
            Write-Host 'DEFERRED: a session is still live - nothing copied'
            exit 1
        }
    }
}

Write-CgEvent 'deploy_start' @{ dest = $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($f in $scripts) {
    Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $Dest $f) -Force
    Write-Host "  $f"
}

# Short rev, '-dirty' when the tree has uncommitted edits. No git degrades to a
# dated 'nogit' stamp, never a failure.
$rev = ''
if (Get-Command git -ErrorAction SilentlyContinue) {
    $out = git -C $PSScriptRoot rev-parse --short HEAD
    if ($LASTEXITCODE -eq 0 -and $out) {
        $rev = "$out".Trim()
        # Scoped to this folder: -dirty must mean the SHIPPED scripts do not
        # match the rev. A scratch file elsewhere in the checkout does not.
        if (git -C $PSScriptRoot status --porcelain .) { $rev += '-dirty' }
    }
}
$stamp = if ($rev) { "$rev $(Get-Date -Format yyyy-MM-dd)" }
         else      { "nogit $(Get-Date -Format yyyy-MM-ddTHH:mm)" }
Set-Content (Join-Path $Dest 'build-id') $stamp
Write-Host "build-id: $stamp"

# Runtime pieces that cannot come from the repo - warn, never touch.
foreach ($f in 'vhui64.exe', 'OFFICE.lnk', 'TV-GAMING.lnk') {
    if (-not (Test-Path (Join-Path $Dest $f))) {
        Write-Host "WARNING: $Dest\$f is missing - install it on this machine (VirtualHere client / DisplayMagician shortcuts)"
    }
}
Write-CgEvent 'deploy_done' @{ scripts = $scripts.Count; build_id = $stamp }
Write-Host "deployed $($scripts.Count) scripts to $Dest"
