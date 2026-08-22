# Unconditional OFFICE restore at every logon (Task \CouchGaming\ForceOfficeAtLogon).
# Normal boots confirm office in one probe and exit; after a crash that left the
# TV-primary topology it applies OFFICE with verified retries. Sends zero TV
# commands - a TV that's off stays off, an Apple TV night stays undisturbed.
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'office-safety'

# An in-flight session task owns the topology - stand down rather than stomp a
# live couch launch (cold-boot corner: K15 dispatches Enter seconds after
# logon; this task fires at logon+20s and would otherwise "recover" it).
if ((Test-CgTaskRunning 'Enter') -or (Test-CgTaskRunning 'Exit')) {
    Log 'Enter/Exit task running - standing down (session flow owns the displays)'
    Stop-Transcript
    exit 0
}

if (-not (Test-TvIsPrimary)) {
    # Fail-open by design: a broken probe reads as "office confirmed" rather
    # than thrashing displays at every logon.
    Log 'office confirmed'
} elseif (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 25 3 'office restored')) {
    Log 'WARNING: OFFICE never took after 3 attempts'
    # The same alertable event Exit-TV emits for the same failure: a desk
    # left in TV topology at logon used to be a transcript line only.
    Write-CgEvent 'profile_apply_failed' @{ profile = 'OFFICE' } 'warn'
}
Stop-DisplayMagician
Clear-ReadyMarker
Clear-OldLogs
Stop-Transcript
