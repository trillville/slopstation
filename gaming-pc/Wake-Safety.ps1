# Resume-from-sleep failsafe (Task \CouchGaming\WakeSafety, on the
# Power-Troubleshooter resume event). Resume is not a logon, so
# ForceOfficeAtLogon never fires here - this cleans up sessions abandoned
# before sleep, while standing down for network wakes (a couch launch owns those).
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'wake-safety'
Start-Sleep 3
$wake = (powercfg /lastwake | Out-String)
Write-Host $wake
if ($wake -match 'Magic Packet|Ethernet|GbE') {
    Log 'network wake - couch launch owns this; standing down'
} elseif (Test-ReadyMarker) {
    Log 'stale TV session detected - running Exit cleanup'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $CG.Root 'Exit-TV.ps1')
} else {
    Log 'clean wake - nothing to do'
}
Stop-Transcript
