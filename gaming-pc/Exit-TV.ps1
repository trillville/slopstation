# Session teardown: close Big Picture, restore OFFICE, release the Puck.
# Runs as Scheduled Task \CouchGaming\Exit (Big Picture tile, Ctrl+Alt+E, or
# Wake-Safety).
# Every step is best-effort and the logon failsafe converges what this misses,
# hence Continue rather than Stop.
$ErrorActionPreference = 'Continue'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'exit'

# Teardown wins: stop an in-flight launch. The steps below reconcile whatever
# half-state the kill leaves.
if (Test-CgTaskRunning 'Enter') {
    Log 'Enter task is running - stopping it (teardown wins)'
    Stop-CgTask 'Enter'
    Start-Sleep 1
    # A killed Enter gets no catch block - clear any DisplayMagician instance it
    # left mid-apply before this script launches its own.
    Stop-DisplayMagician
}

# Leave Big Picture FIRST, while still on the TV, so Steam's window is never
# resolution-yanked mid-render (that leaves a garbled desktop-Steam window).
Start-Process 'steam://close/bigpicture'
Log 'closing Big Picture'
Start-Sleep 2   # blind wait: Big Picture exposes no reliable closed signal

# Office first: controller stays live during teardown
$officeOk = Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 15 2 'ultrawide restored'
if (-not $officeOk) {
    Log 'WARNING: office did not verify - ForceOfficeAtLogon will converge at next logon'
    Write-CgEvent 'profile_apply_failed' @{ profile = 'OFFICE' } 'warn'
} else {
    Write-CgEvent 'profile_applied' @{ profile = 'OFFICE' }
}

$puckOk = Request-PuckRelease 3
if ($puckOk) { Log 'Puck released'; Write-CgEvent 'puck_released' }
else {
    Log 'WARNING: Puck may still be claimed - check VirtualHere client'
    Write-CgEvent 'puck_release_failed' @{} 'warn'
}

# Repaint guard: minimize desktop Steam so it re-lays-out at the ultrawide's
# resolution next time it opens, rather than returning as a stale-4K garbled
# window. (Enter calls this for a different reason - see the lib.)
Hide-DesktopSteam

Clear-ReadyMarker
Log 'done'
Write-CgEvent 'exit_done' @{ office_ok = $officeOk; puck_ok = $puckOk }
Stop-CgTranscript
