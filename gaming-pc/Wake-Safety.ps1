Start-Transcript "C:\CouchGaming\logs\wake-safety-$(Get-Date -Format yyyyMMdd-HHmmss).log"
Start-Sleep 3
$wake = (powercfg /lastwake | Out-String)
Write-Host $wake
if ($wake -match 'Magic Packet|Ethernet|GbE') {
    Write-Host 'network wake - couch launch owns this; standing down'
} elseif ((Test-Path 'C:\ProgramData\CouchGaming\ready')) {
    Write-Host 'stale TV session detected - running Exit cleanup'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\CouchGaming\Exit-TV.ps1'
} else {
    Write-Host 'clean wake - nothing to do'
}
Stop-Transcript