# Session teardown: close Big Picture, restore OFFICE, release the Puck.
# Runs as Scheduled Task \CouchGaming\Exit (Big Picture tile, Ctrl+Alt+E, or
# Wake-Safety).
# Teardown policy: plow through - every step is best-effort and the logon
# failsafe converges anything this misses. Hence no $ErrorActionPreference = 'Stop'.
$ErrorActionPreference = 'Continue'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'exit'

# Conflict rule: teardown wins. If a launch is mid-flight, stop it - this
# script is the reconciler for whatever half-state the kill leaves (office,
# Puck release, ready marker are all handled below).
if (Test-CgTaskRunning 'Enter') {
    Log 'Enter task is running - stopping it (teardown wins)'
    Stop-CgTask 'Enter'
    Start-Sleep 1
    # A killed Enter gets no catch block - clear any DisplayMagician instance it
    # left mid-apply before this script launches its own.
    Stop-DisplayMagician
}

# Leave Big Picture FIRST, while still on the TV - Steam's window never gets
# resolution-yanked mid-render (prevents a garbled desktop-Steam window)
Start-Process 'steam://close/bigpicture'
Log 'closing Big Picture'
Start-Sleep 2   # blind wait by design: Big Picture exposes no reliable closed signal

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

# Repaint guard: minimize desktop Steam so it re-lays-out fresh (at the
# ultrawide's resolution) the next time it's opened - prevents the stale-4K
# garbled window. Shared with Enter, which minimizes it for the OTHER reason
# (see the lib): the desktop library window must never be what holds the
# controller. NOTE this starts actually working now - the version that lived
# here targeted the steam process's MainWindowHandle, which is 0 whenever
# Steam is closed to the tray, so it had never once fired in production.
Hide-DesktopSteam

Clear-ReadyMarker
Log 'done'
Write-CgEvent 'exit_done' @{ office_ok = $officeOk; puck_ok = $puckOk }
Stop-Transcript
