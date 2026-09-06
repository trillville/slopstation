# Deploy the gaming-PC scripts as one checked set and record the build ID.
#
# Run from a checkout, on the PC:
#   powershell -NoProfile -ExecutionPolicy Bypass -File Deploy.ps1
#
# ``-WaitMinutes`` waits for active sessions. This script, Install.ps1 and
# local runtime files are not copied, and config.psd1 is never written.
param([string]$Dest = 'C:\CouchGaming', [int]$WaitMinutes = 0)
$ErrorActionPreference = 'Stop'

$scripts = @(
    'CouchGaming.common.ps1', 'Dispatch.ps1', 'Doctor.ps1',
    'Enter-TV.ps1', 'Exit-TV.ps1', 'Launch-Game.ps1',
    'Nav-BigPicture.ps1', 'Stop-Game.ps1',
    'Office-Safety.ps1', 'Wake-Safety.ps1',
    'config.example.psd1'
)

# This emitter writes directly to the runtime log directory being deployed.
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

# Do not replace scripts while a session task or READY marker is active.
$ready = 'C:\ProgramData\CouchGaming\ready'

function Test-SessionLive {
    if (Test-Path $ready) { return $true }
    foreach ($n in 'Enter', 'Exit') {
        $t = Get-ScheduledTask -TaskPath '\CouchGaming\' -TaskName $n -ErrorAction SilentlyContinue
        if ($t -and $t.State -eq 'Running') { return $true }
    }
    return $false
}

# Refuse incomplete script sets.
$missing = $scripts | Where-Object { -not (Test-Path (Join-Path $PSScriptRoot $_)) }
if ($missing) {
    Write-Host "ABORT: this checkout is missing $($missing -join ', ') - nothing copied"
    exit 1
}

if (Test-SessionLive) {
    if ($WaitMinutes -le 0) {
        Write-Host 'DEFERRED: a session is live - nothing copied (pass -WaitMinutes to wait)'
        exit 1
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
# A failed or interrupted copy must not retain a previous successful build ID.
$buildId = Join-Path $Dest 'build-id'
if (Test-Path $buildId) { Remove-Item $buildId -Force }
foreach ($f in $scripts) {
    $source = Join-Path $PSScriptRoot $f
    $target = Join-Path $Dest $f
    Copy-Item $source $target -Force
    $expected = [Convert]::ToBase64String([IO.File]::ReadAllBytes($source))
    $actual = [Convert]::ToBase64String([IO.File]::ReadAllBytes($target))
    if ($actual -cne $expected) {
        throw "Deployed file differs from source: $f"
    }
    Write-Host "  $f"
}

# Record the short revision and whether deployed files have local changes.
$rev = ''
if (Get-Command git -ErrorAction SilentlyContinue) {
    $out = git -C $PSScriptRoot rev-parse --short HEAD
    if ($LASTEXITCODE -eq 0 -and $out) {
        $rev = "$out".Trim()
        # Only changes to deployed files mark the build dirty.
        if (git -C $PSScriptRoot status --porcelain .) { $rev += '-dirty' }
    }
}
$stamp = if ($rev) { "$rev $(Get-Date -Format yyyy-MM-dd)" }
         else      { "nogit $(Get-Date -Format yyyy-MM-ddTHH:mm)" }
Set-Content $buildId $stamp
Write-Host "build-id: $stamp"

# Runtime pieces that cannot come from the repo - warn, never touch.
foreach ($f in 'vhui64.exe', 'OFFICE.lnk', 'TV-GAMING.lnk') {
    if (-not (Test-Path (Join-Path $Dest $f))) {
        Write-Host "WARNING: $Dest\$f is missing - install it on this machine (VirtualHere client / DisplayMagician shortcuts)"
    }
}
Write-CgEvent 'deploy_done' @{ scripts = $scripts.Count; build_id = $stamp }
Write-Host "deployed $($scripts.Count) files to $Dest"
# The optional git call above may have left a non-zero $LASTEXITCODE.
exit 0
