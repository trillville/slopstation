# Clean up abandoned sessions after resume, except for network wakes used by
# couch launches.
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'wake-safety'
Start-Sleep 3

# Update these patterns after changing the NIC or driver.
$NetworkWakePattern = 'Magic Packet|Ethernet|GbE'

$wake = (powercfg /lastwake | Out-String)
Write-Host $wake
if ($wake -match $NetworkWakePattern) {
    Log 'network wake - couch launch owns this; standing down'
} elseif (Test-ReadyMarker) {
    Log 'stale TV session detected - running Exit cleanup'
    Write-CgEvent 'wake_cleanup' @{ reason = 'stale_session' }
    # Use the task so concurrent teardown requests are serialized.
    schtasks /Run /TN '\CouchGaming\Exit' | Out-Null
} else {
    Log 'clean wake - nothing to do'
}
Clear-OldLogs
Stop-CgTranscript
