# Close Big Picture, restore OFFICE, and release the Puck.
# Continue if a teardown step fails.
$ErrorActionPreference = 'Continue'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'exit'

# Stop an in-progress launch before restoring the shared state.
if (Test-CgTaskRunning 'Enter') {
    Log 'Enter task is running - stopping it (teardown wins)'
    Stop-CgTask 'Enter'
    Start-Sleep 1
    # Clear a DisplayMagician process left by the stopped Enter task.
    Stop-DisplayMagician
}

# Close Big Picture before changing display resolution.
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

# Minimize desktop Steam so it relayouts at the restored resolution.
Hide-DesktopSteam

Clear-ReadyMarker
Log 'done'
Write-CgEvent 'exit_done' @{ office_ok = $officeOk; puck_ok = $puckOk }
Stop-CgTranscript
