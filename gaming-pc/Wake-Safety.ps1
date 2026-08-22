# Resume-from-sleep failsafe (Task \CouchGaming\WakeSafety, on the
# Power-Troubleshooter resume event). Resume is not a logon, so
# ForceOfficeAtLogon never fires here. Cleans up sessions abandoned before
# sleep; stands down for network wakes, which a couch launch owns.
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'wake-safety'
Start-Sleep 3

# Matches this NIC's strings in `powercfg /lastwake` - free-form, English-locale
# text. Re-verify against the raw dump below after any NIC or driver change.
$NetworkWakePattern = 'Magic Packet|Ethernet|GbE'

$wake = (powercfg /lastwake | Out-String)
Write-Host $wake
if ($wake -match $NetworkWakePattern) {
    Log 'network wake - couch launch owns this; standing down'
} elseif (Test-ReadyMarker) {
    Log 'stale TV session detected - running Exit cleanup'
    Write-CgEvent 'wake_cleanup' @{ reason = 'stale_session' }
    # Via the task, not inline: Task Scheduler serializes this against a
    # tile/hotkey Exit, and it leaves the normal exit-*.log trail.
    schtasks /Run /TN '\CouchGaming\Exit' | Out-Null
} else {
    Log 'clean wake - nothing to do'
}
Clear-OldLogs
Stop-CgTranscript
