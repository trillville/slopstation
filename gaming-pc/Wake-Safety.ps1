# Resume-from-sleep failsafe (Task \CouchGaming\WakeSafety, on the
# Power-Troubleshooter resume event). Resume is not a logon, so
# ForceOfficeAtLogon never fires here - this cleans up sessions abandoned
# before sleep, while standing down for network wakes (a couch launch owns those).
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'wake-safety'
Start-Sleep 3

# Matches this NIC's strings in `powercfg /lastwake` output - free-form,
# English-locale text. Verify against the raw dump below after any NIC or
# driver change, and widen if needed.
$NetworkWakePattern = 'Magic Packet|Ethernet|GbE'

$wake = (powercfg /lastwake | Out-String)
Write-Host $wake
if ($wake -match $NetworkWakePattern) {
    Log 'network wake - couch launch owns this; standing down'
} elseif (Test-ReadyMarker) {
    Log 'stale TV session detected - running Exit cleanup'
    # Via the task, not inline: Task Scheduler serializes this against a
    # tile/hotkey Exit, and the cleanup leaves the normal exit-*.log trail.
    schtasks /Run /TN '\CouchGaming\Exit' | Out-Null
} else {
    Log 'clean wake - nothing to do'
}
Stop-Transcript
