# Launch-Game.ps1 - task \CouchGaming\LaunchGame, fired by Dispatch's `launch`
# verb. The appid arrives via the launch-app marker file (schtasks /Run can't
# pass arguments); Dispatch already did the READY/BUSY pre-checks, so this
# script is deliberately dumb: read marker, delete it, re-validate, -applaunch.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'launchgame'
try {
    $marker = $CG.LaunchMarker
    if (-not (Test-Path $marker)) {
        Log 'no launch-app marker - nothing to do'
    } else {
        # Stringify before trimming: Get-Content on an empty file returns $null
        # in PS 5.1, and .Trim() on it would crash BEFORE the delete, stranding
        # the marker. Read, delete, then validate the (possibly empty) string.
        $raw = Get-Content $marker -TotalCount 1
        # Best-effort delete: the marker is written by the ELEVATED sshd
        # forced-command context (owner BUILTIN\Administrators; Users get
        # read-only), and this task runs with the limited token - so the delete
        # is DENIED, and with ErrorActionPreference=Stop that killed the script
        # before -applaunch. Dispatch clears the marker before every write, so
        # one surviving here is overwritten anyway; the only cost is that a
        # MANUAL task run would replay the last appid.
        try { Remove-Item $marker -Force } catch {
            Log 'marker not deletable from this token - Dispatch overwrites it next launch'
        }
        $id = "$raw".Trim()
        if ($id -notmatch '^\d{1,10}$') { throw "invalid appid in marker: '$id'" }
        Log "launching appid $id (-applaunch, into the running Big Picture session)"
        & (Get-SteamExe) -applaunch $id
        Log 'launch handed to Steam'
        Write-CgEvent 'game_launched' @{ appid = [long]$id }
    }
} catch {
    Write-CgEvent 'game_launch_failed' @{ err = "$_" } 'error'
    throw
} finally {
    Stop-Transcript | Out-Null
}
