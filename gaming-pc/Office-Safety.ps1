# Unconditional OFFICE restore at every logon (Task \CouchGaming\ForceOfficeAtLogon).
# Normal boots confirm office in one probe and exit; after a crash that left the
# TV-primary topology it applies OFFICE with verified retries. Sends zero TV
# commands - a TV that's off stays off, an Apple TV night stays undisturbed.
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'office-safety'
if (-not (Test-TvIsPrimary)) {
    # Fail-open by design: a broken probe reads as "office confirmed" rather
    # than thrashing displays at every logon.
    Log 'office confirmed'
} elseif (-not (Invoke-DisplayProfile $CG.OfficeLnk { -not (Test-TvIsPrimary) } 25 3 'office restored')) {
    Log 'WARNING: OFFICE never took after 3 attempts'
}
Stop-DisplayMagician
Clear-ReadyMarker
Stop-Transcript
