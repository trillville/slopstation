# Launch-Game.ps1 - task \CouchGaming\LaunchGame, fired by Dispatch's `launch`
# verb. The appid arrives via the launch-app marker file (schtasks /Run can't
# pass arguments); Dispatch already did the READY/BUSY pre-checks, so this
# script is deliberately dumb: read marker, delete it, re-validate, -applaunch.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'launchgame'
try {
    $marker = 'C:\ProgramData\CouchGaming\launch-app'
    if (-not (Test-Path $marker)) {
        Log 'no launch-app marker - nothing to do'
    } else {
        # Stringify before trimming: Get-Content on an empty file returns $null
        # in PS 5.1, and .Trim() on it would crash BEFORE the delete, stranding
        # the marker. Read, delete, then validate the (possibly empty) string.
        $raw = Get-Content $marker -TotalCount 1
        # Best-effort delete: the marker is written by the ELEVATED sshd
        # forced-command context (owner BUILTIN\Administrators; Users get
        # read-only), and this task runs with the limited token - the delete
        # is DENIED, and with ErrorActionPreference=Stop it killed the script
        # before -applaunch (live 2026-08-10). Dispatch now clears the marker
        # before every write, so one surviving here is overwritten anyway;
        # the only cost is that a MANUAL task run would replay the last appid.
        try { Remove-Item $marker -Force } catch {
            Log 'marker not deletable from this token - Dispatch overwrites it next launch'
        }
        $id = "$raw".Trim()
        if ($id -notmatch '^\d{1,10}$') { throw "invalid appid in marker: '$id'" }
        $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam').SteamPath -replace '/', '\'
        $exe = Join-Path $steam 'steam.exe'
        if (-not (Test-Path $exe)) { throw "steam.exe not found at $exe" }
        Log "launching appid $id (-applaunch, into the running Big Picture session)"
        & $exe -applaunch $id
        Log 'launch handed to Steam'
    }
} finally {
    Stop-Transcript | Out-Null
}
