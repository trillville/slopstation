# Restore the OFFICE display profile at logon when no session is starting.
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'office-safety'

# Do not change displays while a session task is active.
if ((Test-CgTaskRunning 'Enter') -or (Test-CgTaskRunning 'Exit')) {
    Log 'Enter/Exit task running - standing down (session flow owns the displays)'
    Stop-CgTranscript
    exit 0
}

if (-not (Test-TvIsPrimary)) {
    # Avoid changing displays when the probe fails.
    Log 'office confirmed'
} elseif (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 25 3 'office restored') {
    Write-CgEvent 'profile_applied' @{ profile = 'OFFICE' }
} else {
    Log 'WARNING: OFFICE never took after 3 attempts'
    Write-CgEvent 'profile_apply_failed' @{ profile = 'OFFICE' } 'warn'
}
Stop-DisplayMagician
Clear-ReadyMarker
Clear-OldLogs
Stop-CgTranscript
