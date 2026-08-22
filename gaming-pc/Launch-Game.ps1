# Task \CouchGaming\LaunchGame, fired by Dispatch's `launch` verb. The appid
# arrives via the launch-app marker file (schtasks /Run can't pass arguments);
# Dispatch already did the READY/BUSY pre-checks.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'launchgame'
try {
    $marker = 'C:\ProgramData\CouchGaming\launch-app'
    if (-not (Test-Path $marker)) {
        Log 'no launch-app marker - nothing to do'
    } else {
        # Read, delete, then validate. Stringify before trimming: Get-Content on
        # an empty file returns $null in PS 5.1 and .Trim() would throw before
        # the delete, stranding the marker.
        $raw = Get-Content $marker -TotalCount 1
        # Delete is best-effort: the marker is owned by the ELEVATED sshd
        # forced-command context (BUILTIN\Administrators; Users read-only) and
        # this task runs limited, so the delete is DENIED - fatal under
        # ErrorActionPreference=Stop. Dispatch clears it before every write, so
        # a survivor is overwritten; only a MANUAL task run replays the last id.
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
