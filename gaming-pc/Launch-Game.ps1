# Task \CouchGaming\LaunchGame, fired by Dispatch's `launch` verb. The appid
# arrives via the launch-app marker file (schtasks /Run can't pass arguments);
# Dispatch already did the READY/BUSY pre-checks.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CouchGaming.common.ps1"
Start-CgTranscript 'launchgame'
try {
    $id = Read-CgMarker $CG.LaunchMarker
    if ($null -eq $id) {
        Log 'no launch-app marker - nothing to do'
    } else {
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
    Stop-CgTranscript
}
